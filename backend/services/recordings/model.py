"""Call recording: object keys, presigned links, and the one playability rule.

Everything that answers "can this recording be played?" must go through
`resolve_state` here. A second implementation of that arithmetic is how the
calls list ends up advertising a recording the player then 404s on.

**Deletion is a fact, never a derivation.** A recording is gone because
something deleted it and stamped the row — a human through the API
(`RecordingStatus.DELETED`) or a retention purge job (`EXPIRED`). Nothing ages
a row against a window, and no bucket lifecycle rule deletes objects behind
Postgres's back, so the two cannot disagree. That is deliberate: retention is
now `tenants.retention_days`, default unlimited, and a fixed bucket-wide rule
would delete objects the database says are kept forever.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

CONTENT_TYPE = "audio/ogg"
FILE_EXTENSION = "ogg"

# The screen share, when an agent watches one and the author asked for it to be
# kept. A second artifact of the same call,
# with its own status columns, in the same bucket and under the same purge —
# never muxed into the audio, whose file means one thing: stereo, caller left
# and agent right. 1 fps H.264, which is why an hour is single-digit MB.
SCREENSHARE_CONTENT_TYPE = "video/mp4"
SCREENSHARE_FILE_EXTENSION = "mp4"

# How long a minted download link stays valid. Long enough to open a call, read
# the transcript, and then listen to the whole thing — the browser re-requests
# the same URL for every seek, so a link that dies mid-playback would strand a
# player that was working a second earlier. Short enough that a URL copied out
# of a webhook payload is not a lasting credential.
PRESIGNED_TTL = timedelta(hours=1)

# How long after a call ends its recording may still legitimately be uploading.
# Finalize is bounded by `FINALIZE_SHUTDOWN_TIMEOUT_SECONDS` plus the transfer
# itself; past this, a row still saying PENDING is a worker that died rather
# than one still working. Generous on purpose — the cost of being early is
# telling a tenant their audio is lost while it is still on its way.
UPLOAD_GRACE = timedelta(minutes=5)

# How the `stop_recording` tool tells session finalize to drop the file. Session
# userdata is the one thing both sides already hold, and the `_talqing` prefix is
# the existing convention for runtime flags that must not be persisted —
# `finalize_session` strips them before writing.
#
# The value is the ISO moment the caller asked, not a bare `True`: both readers
# only test truthiness, so carrying the timestamp in the same slot costs nothing
# and is the only record of *when* in the call it happened — the session row is
# written afterwards and cannot reconstruct it.
WITHDRAWN_USERDATA_KEY = "_talqing_recording_withdrawn"


class RecordingStatus(StrEnum):
    """What the worker recorded, and what has happened to it since."""

    NONE = "none"
    """Recording was off for this agent, or the session has no audio."""

    PENDING = "pending"
    """The session is live, or finalize is mid-upload."""

    STORED = "stored"
    """The object was uploaded. Playability is `resolve_state`'s call, not this."""

    FAILED = "failed"
    """Recording was on and produced nothing. Deliberately distinct from NONE."""

    NOT_SHARED = "not_shared"
    """Screen recording was on and nobody ever shared a screen.

    Screen-share only — the audio column's CHECK does not allow it, because a
    voice call always has audio. Distinct from NONE, which says the author
    turned it off: one of these is a setting to change and the other is the
    normal outcome of a call where the screen never came up, and a reader
    deciding whether to go and fix something needs to know which."""

    CONSENT_WITHDRAWN = "consent_withdrawn"
    """The caller asked not to be recorded; the file was discarded before upload."""

    DELETED = "deleted"
    """A human deleted it through the API."""

    EXPIRED = "expired"
    """A retention purge deleted it. Distinct from DELETED so a tenant can tell
    their own policy apart from somebody's click."""


class RecordingState(StrEnum):
    """What a reader should do about it. `available` is the only playable one."""

    NONE = "none"
    AVAILABLE = "available"
    PENDING = "pending"
    EXPIRED = "expired"
    FAILED = "failed"
    NOT_SHARED = "not_shared"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    DELETED = "deleted"


def object_key(*, tenant_id: UUID, session_id: str | UUID) -> str:
    """Tenant isolation is the key prefix, matching how the data plane isolates
    by a tenant_id column rather than by separate infrastructure. It also makes
    offboarding a tenant a prefix delete.

    `session_id` is a str on the worker (LiveKit mints it) and a UUID in the API;
    both spell the same key.
    """
    return f"{tenant_id}/{session_id}.{FILE_EXTENSION}"


def screenshare_object_key(*, tenant_id: UUID, session_id: str | UUID) -> str:
    """The screen recording, beside the audio under the same tenant prefix.

    The `-screen` suffix rather than a second directory level so a tenant
    offboarding stays one prefix delete, exactly as `object_key` intends.
    """
    return f"{tenant_id}/{session_id}-screen.{SCREENSHARE_FILE_EXTENSION}"


def download_filename(session_id: str | UUID) -> str:
    return f"call-{session_id}.{FILE_EXTENSION}"


def screenshare_download_filename(session_id: str | UUID) -> str:
    return f"call-{session_id}-screen.{SCREENSHARE_FILE_EXTENSION}"


def resolve_state(
    status: str, started_at: datetime | None, *, session_ended_at: datetime | None
) -> RecordingState:
    """Fold the stored status and the session's own end into one answer.

    A STORED row with no `started_at` reads as FAILED: the upload wrote a key
    without the timestamp the transcript-sync arithmetic needs, which is a bug,
    and pretending the audio is available would hide it.

    `session_ended_at` is required rather than optional because it decides the
    one case the status alone gets wrong. PENDING means "the session is live, or
    finalize is mid-upload" — but a worker that dies never writes the outcome,
    so the row sits at PENDING forever and every reader goes on saying "the call
    is still in progress" about a call that ended days ago. Once the session has
    a terminal timestamp and the upload window has passed, the audio is not
    coming.

    The window exists because finalize writes the session row BEFORE it ships
    the recording (`workers/voice/runtime.py::_finalize_once`), so there is a
    legitimate span where the call has ended and the upload is genuinely still
    running. Reading that as a failure would flash "could not be saved" on every
    normal call.
    """
    if status == RecordingStatus.PENDING and session_ended_at is not None:
        if datetime.now(UTC) - session_ended_at > UPLOAD_GRACE:
            return RecordingState.FAILED
        return RecordingState.PENDING

    if status != RecordingStatus.STORED:
        # The remaining statuses map to states of the same name.
        return RecordingState(status)

    if started_at is None:
        return RecordingState.FAILED

    return RecordingState.AVAILABLE
