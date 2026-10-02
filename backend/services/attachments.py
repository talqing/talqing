"""Images people attach to a conversation: ingest, storage shape, read links.

One function ingests, whatever the channel: a byte stream on a web voice or
video call (`workers/voice/images.py`), or a data URL on a text message
(`services.chats.service.accept_message`). Everything downstream — the
stored shape, the retention purge, the transcript renderers — is
channel-agnostic, which is what keeps a future Telegram photo a matter of
downloading the file and calling `ingest_image`.

**The transcript row is the record.** `conversation_items.attachments` holds the
object key plus the metadata a reader needs; there is no attachments table,
because the conversation item is already the durable record of what was said and
a second table beside it would be a second place for the same fact to go stale.

**The key is stored and never returned.** The API answers with a presigned URL
minted per request, exactly as a recording does: a tenant cannot use a key
against a private bucket, and shipping one invites them to try.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from services import storage
from services.user import Tenant
from settings import BucketConfig, get_settings

logger = logging.getLogger("talqing.services.attachments")

# Decoded bytes, before we re-encode. Above this we refuse rather than resize:
# a 10 MB photo is already far past anything a model gains from, and accepting
# more only moves the cost from the sender to the turn.
IMAGE_MAX_BYTES = 10 * 1024 * 1024
# A base64 payload expands by 4/3; anything past that cannot decode under the
# cap, so it is refused before it is decoded rather than after.
DATA_URL_MAX_CHARS = IMAGE_MAX_BYTES * 4 // 3 + 1024
# Per text message, and per conversation over its whole life.
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGES_PER_CONVERSATION = 100
# Longest side of the stored image. Above this no provider gains anything and
# every turn that replays the image pays for it.
MAX_IMAGE_EDGE_PX = 1568

# How long a read link lives. Matches the recording link for the same reason:
# long enough to read a transcript through, short enough that a URL copied out
# of a page is not a lasting credential. `url_expires_at` is returned so a page
# left open past it can offer a reload rather than a broken image.
PRESIGNED_TTL = timedelta(hours=1)

# Decided from the DECODED image, never from what the sender declared. GIF is
# refused although LiveKit would carry it: an animated GIF is cost with no
# support use case.
_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
_MIME_EXTENSION = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
ACCEPTED_TYPES = "JPEG, PNG or WebP"


class ImageRejected(ValueError):
    """An image we will not take, with a sentence explaining why.

    The message is rendered verbatim — to a caller as an HTTP error, to a browser
    on a live call as the reason a thumbnail turned red. Write it for a person:
    "images are 10 MB or smaller", never a status code or an exception repr.
    """


class ImageAttachment(BaseModel):
    """One image as stored on `conversation_items.attachments`.

    ``kind`` is the discriminator a later file type would use; today every
    attachment is an image.
    """

    id: UUID
    kind: Literal["image"] = "image"
    object_key: str
    mime_type: str
    bytes: int
    width: int
    height: int
    filename: str | None = None


class ConversationAttachment(BaseModel):
    """One image as the API returns it: the same record, signed, minus the key."""

    id: UUID
    kind: Literal["image"] = "image"
    mime_type: str
    bytes: int
    width: int
    height: int
    filename: str | None = None
    # Minted per request. A page open past `url_expires_at` should offer a
    # reload rather than render a broken image.
    url: str
    url_expires_at: datetime


def bucket() -> BucketConfig:
    return get_settings().storage.attachments


def object_key(*, tenant_id: UUID, conversation_id: UUID, image_id: UUID, mime_type: str) -> str:
    """`{tenant}/{conversation}/{image}.{ext}` — derived here, never from a client.

    Tenant first, matching `recordings.object_key`, so the purge job's
    `startswith(f"{tenant.id}/")` guard applies unchanged. The conversation
    segment is what makes cleaning up a deleted thread a prefix delete.
    """
    return f"{tenant_id}/{conversation_id}/{image_id}.{_MIME_EXTENSION[mime_type]}"


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Split a `data:<mime>;base64,<payload>` URL into its bytes and its claim.

    The mime is what the sender said, not what the image is — step 2 of
    `ingest_image` decides that from the pixels. It is kept because "the browser
    said PNG and Pillow saw something else" is the sentence that explains a
    refusal.
    """
    if not data_url.startswith("data:") or ";base64," not in data_url:
        raise ImageRejected("an image must be sent as a base64 data URL")
    if len(data_url) > DATA_URL_MAX_CHARS:
        raise ImageRejected("images are 10 MB or smaller")
    header, payload = data_url.split(";base64,", 1)
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageRejected("that image is not valid base64") from exc
    return data, header[len("data:") :]


def _normalize(data: bytes) -> tuple[bytes, str, int, int]:
    """Decode → EXIF-transpose → strip metadata → resize → re-encode.

    Runs on a worker thread (see `ingest_image`): decoding and resampling a
    10 MB photo is hundreds of milliseconds of CPU, and on the voice worker that
    is the event loop nine other live calls are sharing.

    Skipping the transpose is the classic bug in this function. Stripping EXIF is
    what protects the sender — a phone photo carries GPS — and the orientation
    tag we would be stripping is the thing that was holding a portrait photo the
    right way up, so the rotation has to be baked into the pixels first.
    """
    try:
        with Image.open(BytesIO(data)) as opened:
            fmt = (opened.format or "").upper()
            if fmt not in _FORMAT_MIME:
                raise ImageRejected(f"that file is not a {ACCEPTED_TYPES}")
            image = ImageOps.exif_transpose(opened) or opened
            image.load()
    except ImageRejected:
        raise
    except Exception as exc:
        # A truncated upload, a decompression bomb past Pillow's pixel ceiling,
        # or a PDF renamed .png. One refusal for all three: the sender's next
        # move is the same in every case.
        raise ImageRejected(f"that file could not be read as a {ACCEPTED_TYPES} image") from exc

    mime = _FORMAT_MIME[fmt]
    longest = max(image.size)
    if longest > MAX_IMAGE_EDGE_PX:
        scale = MAX_IMAGE_EDGE_PX / longest
        image = image.resize(
            (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
            Image.Resampling.LANCZOS,
        )

    # Nothing from the source's metadata travels with the pixels: EXIF and its
    # GPS tags, the ICC profile, PNG text chunks, XMP. Pillow's encoders read
    # each of those off `info` when the caller passes none, so emptying it is
    # what actually strips them — `save()` alone does not.
    image.info = {}
    if fmt == "JPEG" and image.mode not in ("RGB", "L"):
        # A transposed CMYK or palette JPEG comes back in a mode the encoder
        # refuses; the alternative is a 500 on a file that is perfectly valid.
        image = image.convert("RGB")

    out = BytesIO()
    if fmt == "JPEG":
        image.save(out, "JPEG", quality=85, optimize=True)
    elif fmt == "PNG":
        image.save(out, "PNG", optimize=True)
    else:
        image.save(out, "WEBP", quality=85)
    return out.getvalue(), mime, image.width, image.height


async def ingest_image(
    conn: asyncpg.Connection,
    tenant: Tenant,
    conversation_id: UUID,
    data: bytes,
    *,
    declared_mime: str | None,
    filename: str | None,
) -> tuple[ImageAttachment, bytes]:
    """Validate, normalize and store one image. Every failure is `ImageRejected`.

    Returns the record and the normalized bytes. The bytes are handed back
    because the caller on a live call is about to put this exact image in front
    of the model, and re-reading it from the bucket to do that would be a round
    trip for something already in memory.

    ``conn`` rather than a pool: the per-conversation ceiling is only correct
    while the caller holds whatever serializes concurrent senders — the
    conversation row's `FOR UPDATE` on the text path, the handler's lock on the
    room path — and taking a second connection would read outside it.

    The upload finishes before this returns, and so before the image enters the
    model's view (D11): every partial state where the model can answer about an
    image the transcript does not have is worth more than the round trip costs.
    """
    if len(data) > IMAGE_MAX_BYTES:
        raise ImageRejected("images are 10 MB or smaller")
    if not data:
        raise ImageRejected("that image is empty")

    payload, mime, width, height = await asyncio.to_thread(_normalize, data)
    if declared_mime and declared_mime != mime:
        logger.info(
            "image declared %s but decoded as %s (conversation %s)",
            declared_mime,
            mime,
            conversation_id,
        )

    held = await conn.fetchval(
        """
        SELECT coalesce(sum(jsonb_array_length(attachments)), 0)
        FROM conversation_items
        WHERE tenant_id = $1 AND conversation_id = $2
        """,
        tenant.id,
        conversation_id,
    )
    if held >= MAX_IMAGES_PER_CONVERSATION:
        raise ImageRejected(f"this conversation already holds {MAX_IMAGES_PER_CONVERSATION} images")

    image_id = uuid4()
    key = object_key(
        tenant_id=tenant.id,
        conversation_id=conversation_id,
        image_id=image_id,
        mime_type=mime,
    )
    try:
        await storage.upload_object(bucket(), key=key, fileobj=BytesIO(payload), content_type=mime)
    except Exception as exc:
        logger.exception("image upload failed for conversation %s", conversation_id)
        raise ImageRejected("that image could not be stored — try again") from exc

    return (
        ImageAttachment(
            id=image_id,
            object_key=key,
            mime_type=mime,
            bytes=len(payload),
            width=width,
            height=height,
            filename=(filename or "").strip() or None,
        ),
        payload,
    )


async def discard(records: Iterable[ImageAttachment]) -> None:
    """Delete images stored for a message that then failed to be written.

    An upload happens before the row that names it — that is what stops the
    model answering about an image the transcript does not have — so a refused
    fifth image, or an INSERT that rolls back, leaves the earlier ones in the
    bucket with nothing pointing at them. Best-effort and never raising: it runs
    while an error is already on its way to the caller, and a leaked object is a
    far smaller problem than replacing their error with ours.
    """
    for record in records:
        try:
            await storage.delete_object(bucket(), key=record.object_key)
        except Exception:
            logger.warning("could not discard orphaned image %s", record.object_key, exc_info=True)


Reader = Callable[[str], Awaitable[bytes]]


@asynccontextmanager
async def reader() -> AsyncIterator[Reader]:
    """Read several stored images back inside one S3 client.

    The same shape as `signer`, and here it earns more: a cold start on a long
    thread fetches every image the conversation holds, and a client per image
    would be a TLS handshake and a connection pool per image against a bucket
    that is a Pacific crossing away. One client, N concurrent GETs on it.
    """
    async with storage.s3_client(bucket()) as client:

        async def read(key: str) -> bytes:
            response = await client.get_object(Bucket=bucket().name, Key=key)
            async with response["Body"] as body:
                return bytes(await body.read())

        yield read


def data_url(mime_type: str, raw: bytes) -> str:
    """The one form of an image every provider we run accepts.

    Never a presigned URL (D5): LiveKit maps an external URL to Gemini's
    `file_data.file_uri`, which takes Files-API and GCS URIs only, and OpenAI's
    realtime plugin refuses external URLs outright. Inlining also means an
    expiring link can never be the reason a turn failed.
    """
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


def stored(raw: object) -> list[ImageAttachment]:
    """Parse the JSONB column. A row we cannot read is a bug, so it raises."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise TypeError("conversation_items.attachments must be a JSON array")
    return [ImageAttachment.model_validate(entry) for entry in raw]


Signer = Callable[[object], Awaitable[list[ConversationAttachment]]]


@asynccontextmanager
async def signer(tenant_id: UUID) -> AsyncIterator[Signer]:
    """Sign a whole page of items' attachments inside one S3 client.

    Presigning is local HMAC and costs nothing; opening a botocore client per
    item is what would hurt. The client is opened on the first attachment
    actually seen, so a page with none — which is nearly every page — pays
    nothing at all.

    ``tenant_id`` is checked against every key before it is signed. Every row
    reaching here was already fetched with ``tenant_id`` in the WHERE, so a key
    from another workspace should be unreachable — this checks anyway, for the
    reason ``services.calls._tenant_object_key`` checks the recording key: the
    thing on the other end is a photo somebody's customer sent, one bucket holds
    every workspace's, and if a bad key is ever written the failure has to be a
    loud 500 rather than one tenant quietly reading another's image.
    """
    client: Any = None
    async with AsyncExitStack() as stack:

        async def sign(raw: object) -> list[ConversationAttachment]:
            nonlocal client
            items = stored(raw)
            if not items:
                return []
            for item in items:
                if not item.object_key.startswith(f"{tenant_id}/"):
                    raise ValueError(
                        f"attachment key {item.object_key!r} is outside tenant {tenant_id}"
                    )
            if client is None:
                client = await stack.enter_async_context(storage.s3_client(bucket()))
            expires_at = datetime.now(UTC) + PRESIGNED_TTL
            out: list[ConversationAttachment] = []
            for item in items:
                url = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket().name, "Key": item.object_key},
                    ExpiresIn=int(PRESIGNED_TTL.total_seconds()),
                )
                out.append(
                    ConversationAttachment(
                        **item.model_dump(exclude={"object_key"}),
                        url=str(url),
                        url_expires_at=expires_at,
                    )
                )
            return out

        yield sign


class InboundImage(BaseModel):
    """One image on an inbound text message: the bytes, and what to call them."""

    data_url: str = Field(min_length=1, max_length=DATA_URL_MAX_CHARS)
    filename: str | None = Field(default=None, max_length=255)
