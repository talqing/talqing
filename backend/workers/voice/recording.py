"""Ship the session's audio file to object storage at finalize.

`AgentSession` records into `job_ctx.session_directory`, a `TemporaryDirectory`
that dies with the job process — so the upload has to happen inside finalize,
before teardown, or the file is gone.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiofiles
import av
from livekit.agents import AgentSession, JobContext, RecordingOptions

from services import recordings
from services.agents import AgentConfig
from services.recordings import RecordingStatus
from services.tools import UserData
from services.user import Tenant

logger = logging.getLogger("talqing.workers.voice.recording")

# The upload's budget, in two parts: a fixed allowance for connecting and for a
# small file, plus time proportional to the bytes. A single number cannot serve
# both ends of the range — 48 kHz stereo Opus runs ~580 kB/min, so a 5-minute
# call is ~3 MB and the 3h cap (`livekit.sip.max_call_duration_seconds`) is
# ~105 MB, and any constant generous enough for the second lets the first hang.
#
# The throughput floor is what the timeout actually asserts: below it, the
# bucket is not going to finish and the call should stop waiting.
UPLOAD_TIMEOUT_BASE_SECONDS = 10.0
UPLOAD_MIN_THROUGHPUT_MB_S = 4.0
# The worst case a legal call can produce, which is what
# `VoiceRun.FINALIZE_SHUTDOWN_TIMEOUT_SECONDS` has to contain.
MAX_RECORDING_MB = 110.0
MAX_UPLOAD_TIMEOUT_SECONDS = (
    UPLOAD_TIMEOUT_BASE_SECONDS + MAX_RECORDING_MB / UPLOAD_MIN_THROUGHPUT_MB_S
)


def upload_timeout_seconds(size_bytes: int) -> float:
    return UPLOAD_TIMEOUT_BASE_SECONDS + (size_bytes / 1024 / 1024) / UPLOAD_MIN_THROUGHPUT_MB_S


def recording_options(cfg: AgentConfig) -> RecordingOptions:
    """What to pass as `AgentSession.start(record=...)`.

    Every key is stated because a partial dict is merged over "all on", not over
    "all off" — `{"audio": True}` would quietly also enable traces, logs and
    transcript upload, all of which ship to LiveKit Cloud. We self-host; the only
    thing we want from this machinery is the local audio file.

    Note the CPU this costs: RecorderIO encodes 48 kHz stereo Opus at libopus'
    default complexity, ~22 ms of CPU per second of call (roughly double that on
    a DO shared vCPU). Ten concurrent recorded calls is a meaningful slice of the
    voice worker's 1-vCPU limit. Cutting it needs RecorderIO to expose encoder
    options upstream.
    """
    return {
        "audio": cfg.recording.enabled,
        "traces": False,
        "logs": False,
        "transcript": False,
        "redaction": False,
    }


@dataclass(frozen=True)
class RecordingOutcome:
    status: RecordingStatus
    object_key: str | None = None
    started_at: datetime | None = None
    duration_s: int | None = None
    size_bytes: int | None = None


def _probe_duration_s(path: Path) -> int | None:
    """Real container duration, so the number we show matches the player's.

    Wall clock would be cheaper but wrong: the recorder writes in whole Opus
    frames on its own flush cadence, and its timeline runs from `start()` to
    `aclose()` rather than from the call's own endpoints.
    """
    try:
        with av.open(str(path)) as container:
            if container.duration is None:
                return None
            return round(container.duration / av.time_base)
    except Exception:
        # A duration we cannot read is worth a log and a NULL. It is never worth
        # storing a guess that disagrees with what the browser will play.
        logger.warning("could not probe recording duration at %s", path, exc_info=True)
        return None


async def upload_session_recording(
    tenant: Tenant,
    session_id: str,
    *,
    session: AgentSession[UserData],
    job_ctx: JobContext | None,
    withdrawn: bool,
) -> RecordingOutcome:
    """Read the finished recording off disk and put it in the bucket.

    Call only after `AgentSession.aclose()`: that is what closes `RecorderIO` and
    flushes the last Opus packets. Reading earlier yields a truncated file.
    """
    if withdrawn:
        # The caller asked not to be recorded. There is nothing to delete — the
        # file only ever existed in the job process's tempdir.
        return RecordingOutcome(status=RecordingStatus.CONSENT_WITHDRAWN)

    if job_ctx is None:
        # No job context means no `session_directory`, so `AgentSession` never
        # wired the recorder at all (agent_session.py gates the whole block on
        # `if job_ctx:`). Every voice path we run today is a LiveKit job, so this
        # is 'failed' rather than 'none': the agent asked to be recorded and
        # there is nothing to show for it.
        logger.error("session %s: recording enabled but the run has no job context", session_id)
        return RecordingOutcome(status=RecordingStatus.FAILED)

    # Private attribute by necessity: `RecorderIO` is not exposed on the public
    # AgentSession surface, and `make_session_report()` — the documented route —
    # copies the whole chat history to hand back the same two values. If a future
    # SDK renames this, the AttributeError surfaces as a 'failed' recording and a
    # stack trace, which is the loud failure we want rather than silent silence.
    recorder = session._recorder_io
    if recorder is None or recorder.output_path is None:
        logger.warning("session %s: recording was enabled but no file was produced", session_id)
        return RecordingOutcome(status=RecordingStatus.FAILED)

    path = Path(recorder.output_path)
    started_at = (
        datetime.fromtimestamp(recorder.recording_started_at, UTC)
        if recorder.recording_started_at is not None
        else None
    )

    size_bytes = path.stat().st_size if path.exists() else 0
    if size_bytes == 0:
        # A call that connected and hung up before anyone spoke.
        logger.info("session %s: recording file is empty, nothing to upload", session_id)
        return RecordingOutcome(status=RecordingStatus.FAILED, started_at=started_at)

    if started_at is None:
        # It is the origin the transcript-sync arithmetic measures from — an
        # item's offset into the file is `created_at - recording_started_at` —
        # so a recording without one is unusable even though the bytes are fine.
        # Better to say so than to serve audio nothing can line up.
        logger.error("session %s: recording has no start time, refusing to store", session_id)
        return RecordingOutcome(status=RecordingStatus.FAILED)

    key = recordings.object_key(tenant_id=tenant.id, session_id=session_id)
    try:
        duration_s = await asyncio.to_thread(_probe_duration_s, path)
        # Streamed, never `read_bytes()`: the 3h cap is a ~105 MB `bytes` object
        # in a job process whose pod is limited to 3 Gi, and up to ten of them
        # can be finalizing at once.
        async with aiofiles.open(path, "rb") as fileobj:
            await asyncio.wait_for(
                recordings.upload_object(
                    key=key, fileobj=fileobj, content_type=recordings.CONTENT_TYPE
                ),
                timeout=upload_timeout_seconds(size_bytes),
            )
    except Exception:
        # Timeout included. Never let a slow or broken bucket hold a session
        # open: the row says 'failed', which is queryable, and the call finishes
        # hanging up.
        logger.exception("session %s: recording upload failed", session_id)
        return RecordingOutcome(status=RecordingStatus.FAILED, started_at=started_at)

    return RecordingOutcome(
        status=RecordingStatus.STORED,
        object_key=key,
        started_at=started_at,
        duration_s=duration_s,
        size_bytes=size_bytes,
    )
