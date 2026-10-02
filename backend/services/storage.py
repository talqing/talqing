"""Object storage — DigitalOcean Spaces, every environment.

Two buckets live behind this module: call recordings and conversation image
attachments. Every function takes the ``BucketConfig`` it should act on, and
that value carries the credentials as well as the name — DigitalOcean
limited-access keys are scoped to one bucket, so a bare bucket name beside a
single shared key pair would open a client for one bucket holding the other's
key and fail as a 403 mid-call.

One `aioboto3.Session` for the process; boto3 sessions are cheap to hold and
expensive to rebuild, and every client is opened as a context manager because
aiobotocore keeps a connection pool per client that has to be closed.

**The API never carries the bytes.** It answers with a short-lived presigned URL
and the browser fetches the object straight from the bucket, which is what makes
`Range` work for a recording: the player seeks by asking for a byte range, and
the bucket serves that natively where the API would have had to stream the whole
file first. The cost is that a bucket needs a CORS rule for the dashboard origin
— the player splits the stereo channels through the Web Audio API, and
`createMediaElementSource` on a cross-origin element the bucket has not
CORS-approved yields a graph that outputs silence. That rule is set by hand in
the Spaces control panel. Only the
recordings bucket carries it: attachments are rendered by a plain `<img>`, which
is not a CORS request at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import IO, Any

import aioboto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from settings import BucketConfig, get_settings

_session: aioboto3.Session | None = None

# Above this, `upload_fileobj` switches from one PUT to a multipart upload. Also
# the size of the first read, so it bounds what the job process holds in memory
# for a small file — the whole point of streaming rather than `read_bytes()`.
MULTIPART_THRESHOLD_BYTES = 8 * 1024 * 1024


# `Any`: aiobotocore builds its clients at runtime from botocore's service model,
# so there is no real type to name without pulling in the types-aiobotocore stub
# package for one annotation.
@asynccontextmanager
async def s3_client(bucket: BucketConfig) -> AsyncIterator[Any]:
    """Open an S3 client holding this bucket's own credentials.

    One endpoint, used by the API, the workers and — through the URLs it signs —
    the browser. SigV4 signs the Host header, so a URL minted here cannot be
    rewritten for a different address afterwards; anything needing a second
    address needs a second bucket, not a second name for this one.

    `addressing_style="path"` is what `force_path_style` means to botocore.
    Spaces serves browsers path-style and reserves virtual-host style for API
    access; getting it wrong fails as a DNS error, which reads like a network
    problem rather than a config one.
    """
    global _session
    if _session is None:
        _session = aioboto3.Session()

    cfg = get_settings().storage
    async with _session.client(
        "s3",
        endpoint_url=cfg.endpoint_url,
        region_name=cfg.region,
        aws_access_key_id=bucket.access_key,
        aws_secret_access_key=bucket.secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if cfg.force_path_style else "auto"},
            # The recording upload runs inside the session-finalize budget, so a
            # stalled bucket must give up rather than hold the job process open.
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    ) as client:
        yield client


async def upload_object(
    bucket: BucketConfig, *, key: str, fileobj: IO[bytes], content_type: str
) -> None:
    """Stream one open file into the bucket, multipart above the threshold.

    Never `read_bytes()`: at ~580 kB of Opus per minute the 3h call cap is a
    ~105 MB `bytes` object, held in a job process whose pod is limited to 3 Gi
    while nine other calls may be finalizing beside it.

    `fileobj` may be a plain binary file, an `aiofiles` handle or a `BytesIO` —
    aioboto3 awaits `read()` only when it returns an awaitable, so all three work
    and the async one keeps the disk reads off the event loop.
    """
    async with s3_client(bucket) as client:
        await client.upload_fileobj(
            fileobj,
            bucket.name,
            key,
            ExtraArgs={"ContentType": content_type},
            Config=TransferConfig(multipart_threshold=MULTIPART_THRESHOLD_BYTES),
        )


async def delete_object(bucket: BucketConfig, *, key: str) -> None:
    async with s3_client(bucket) as client:
        await client.delete_object(Bucket=bucket.name, Key=key)


async def delete_objects(bucket: BucketConfig, *, keys: Sequence[str]) -> None:
    """Delete many keys in one request. Idempotent, like a single delete.

    S3 takes 1000 keys per `delete_objects`, so a conversation's whole set is
    one round trip on one client rather than N of each.
    """
    if not keys:
        return
    async with s3_client(bucket) as client:
        for start in range(0, len(keys), 1000):
            batch = [{"Key": key} for key in keys[start : start + 1000]]
            await client.delete_objects(Bucket=bucket.name, Delete={"Objects": batch})


async def delete_prefix(bucket: BucketConfig, *, prefix: str) -> None:
    """Delete every object under one prefix. Idempotent, like a single delete.

    Paginated because `list_objects_v2` caps at 1000 keys, and batched into
    `delete_objects` because a conversation of a hundred images should not be a
    hundred round trips.
    """
    async with s3_client(bucket) as client:
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=bucket.name, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                await client.delete_objects(Bucket=bucket.name, Delete={"Objects": keys})


async def presigned_url(
    bucket: BucketConfig, *, key: str, expires_in: int, attachment_filename: str | None = None
) -> str:
    """A signed GET link the browser can use directly.

    `attachment_filename` signs `response-content-disposition` into the URL so
    the bucket names the file itself. That has to come from the bucket rather
    than from the page, because the recording player's Download control is an
    `<a download>` and **the `download` attribute is ignored cross-origin** —
    left to the browser the file would save under a signature blob of a name.

    One disposition per URL: a link minted for playback must not be the same one
    handed to a Download control, or the two behaviours become one guess.
    """
    params: dict[str, str] = {"Bucket": bucket.name, "Key": key}
    if attachment_filename is not None:
        params["ResponseContentDisposition"] = f'attachment; filename="{attachment_filename}"'
    async with s3_client(bucket) as client:
        url = await client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)
    return str(url)
