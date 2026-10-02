"""Call recording: the object layout, the bucket, and the playability rule.

Import what you need from here::

    from services import recordings
    key = recordings.object_key(tenant_id=..., session_id=...)
    state = recordings.resolve_state(row["recording_status"], row["recording_started_at"],
                                     session_ended_at=row["ended_at"])

`resolve_state` is the single place that decides whether a recording can be
played — see `model.py` for why a second copy of that rule is a bug.

The three storage calls are `services.storage` bound to the recordings bucket,
so a caller here never has to name a bucket and cannot name the wrong one.
"""

from __future__ import annotations

from typing import IO

from services import storage
from settings import BucketConfig, get_settings

from .model import (  # noqa: F401
    CONTENT_TYPE,
    FILE_EXTENSION,
    PRESIGNED_TTL,
    SCREENSHARE_CONTENT_TYPE,
    SCREENSHARE_FILE_EXTENSION,
    WITHDRAWN_USERDATA_KEY,
    RecordingState,
    RecordingStatus,
    download_filename,
    object_key,
    resolve_state,
    screenshare_download_filename,
    screenshare_object_key,
)


def _bucket() -> BucketConfig:
    return get_settings().storage.recordings


async def upload_object(*, key: str, fileobj: IO[bytes], content_type: str) -> None:
    await storage.upload_object(_bucket(), key=key, fileobj=fileobj, content_type=content_type)


async def delete_object(*, key: str) -> None:
    await storage.delete_object(_bucket(), key=key)


async def presigned_url(
    *, key: str, expires_in: int, attachment_filename: str | None = None
) -> str:
    return await storage.presigned_url(
        _bucket(), key=key, expires_in=expires_in, attachment_filename=attachment_filename
    )


__all__ = [
    "CONTENT_TYPE",
    "FILE_EXTENSION",
    "PRESIGNED_TTL",
    "SCREENSHARE_CONTENT_TYPE",
    "SCREENSHARE_FILE_EXTENSION",
    "WITHDRAWN_USERDATA_KEY",
    "RecordingState",
    "RecordingStatus",
    "delete_object",
    "download_filename",
    "object_key",
    "presigned_url",
    "resolve_state",
    "screenshare_download_filename",
    "screenshare_object_key",
    "upload_object",
]
