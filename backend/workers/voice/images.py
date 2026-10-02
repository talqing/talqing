"""Images a caller sends during a web voice or video call.

The browser calls `room.localParticipant.sendFile(file, {topic: "talqing.images",
destinationIdentities: [agentIdentity]})` and this handler receives the bytes —
LiveKit's documented path, already authenticated by the room, and one hop from
the sender's disk into the agent's hands. There is no upload endpoint and no RPC
handing over an id.

**The agent does not answer the photo.** It goes into the model's view and the
handler stops: the caller is mid-conversation and about to say what the photo is
for, and speaking unprompted would talk over them. Their next turn is answered
with the image already in context, which is the interaction they expected.

A byte stream is one-way, so the reply channel is an RPC back to the sender,
`talqing.image_result`. Two things that would normally make that fiddly are
handed to us by the stream itself: the handler's second argument IS the sender's
identity, and the id `sendFile` returned to the browser IS `info.stream_id` on
this side — so there is no participant to pick and no correlation map to keep.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

from livekit import rtc
from livekit.agents import llm, utils

import db
from services import attachments
from services.attachments import ImageAttachment, ImageRejected
from services.catalog import get_catalog
from workers.session.runtime_kind import item_source_for_session_type
from workers.voice.runtime import VoiceRun

logger = logging.getLogger("talqing.workers.voice.images")

IMAGE_TOPIC = "talqing.images"
ACK_METHOD = "talqing.image_result"


def agent_reads_images(cfg: object) -> bool:
    """Whether the model this agent runs can see an image at all.

    The only gate there is: image input is a property of the model, not a
    setting an author turns on. False is a real answer today — both grok-voice
    realtime models take an image, drop it silently, and answer anyway.
    """
    spec = getattr(cfg, "realtime", None)
    kind = "realtime" if spec is not None else "llm"
    if spec is None:
        spec = getattr(cfg, "llm", None)
    if spec is None:
        return False
    entry = get_catalog().entry(kind, spec.provider, spec.model)
    return bool(getattr(entry, "vision", False))


def register_image_handler(room: rtc.Room, run: VoiceRun) -> None:
    """Take `talqing.images` byte streams for this call.

    A web room always has a conversation — `calls_token` mints one before the
    dispatch — so its absence is a bug, asserted here rather than defended
    against at every use below.
    """
    assert run.spec.conversation_id, "a web room run must have a conversation"
    conversation_id = UUID(run.spec.conversation_id)
    tenant = run.spec.tenant
    can_see = agent_reads_images(run.cfg)
    # `update_chat_ctx` is copy-modify-write, so two images arriving together
    # would each copy the same context and the second write would drop the
    # first's message — an image that uploaded, persisted, and is invisible to
    # the model. The same lock is what makes `ingest_image`'s per-conversation
    # ceiling correct on this path.
    lock = asyncio.Lock()
    # The docs' task-retention rule: the handler LiveKit calls is synchronous, so
    # without a strong reference the read task can be collected mid-stream.
    tasks: set[asyncio.Task[None]] = set()

    async def _ack(identity: str, stream_id: str, error: str | None) -> None:
        """Tell the browser what became of an image it sent.

        Best-effort, and the `try` is required rather than defensive habit:
        `perform_rpc` raises UNSUPPORTED_METHOD against a client that never
        registered the method, and a tenant's own frontend is entitled not to.

        `error` is a sentence for a person — it is rendered verbatim.
        """
        try:
            await room.local_participant.perform_rpc(
                destination_identity=identity,
                method=ACK_METHOD,
                payload=json.dumps({"stream_id": stream_id, "ok": error is None, "error": error}),
                response_timeout=5,
            )
        except Exception:
            logger.debug("image ack not delivered to %s", identity, exc_info=True)

    async def _read(reader: rtc.ByteStreamReader) -> bytes:
        """Read the whole stream, refusing past the cap.

        Measured while reading rather than taken from `ByteStreamInfo.size`,
        which the client declares and can lie about.
        """
        chunks: list[bytes] = []
        total = 0
        async for chunk in reader:
            total += len(chunk)
            if total > attachments.IMAGE_MAX_BYTES:
                raise ImageRejected("images are 10 MB or smaller")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _persist(record: ImageAttachment, item_id: str) -> None:
        """Write the row in full — nothing else will.

        The message goes into the context through `update_chat_ctx`, which fires
        no `conversation_item_added`, so the live transcript listener never sees
        it and there is no second writer to fill anything in afterwards.
        """
        pool = await db.tenant_pool(tenant)
        agent_id, agent_version = run.current_agent_stamp()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO conversation_items (
                    tenant_id, conversation_id, session_id,
                    direction, type, role, agent_id, agent_version,
                    text, attachments, source, visibility, metadata
                )
                VALUES ($1, $2, $3, 'inbound', 'message', 'user', $4::uuid, $5,
                        NULL, $6::jsonb, $7, 'customer_visible', $8::jsonb)
                """,
                tenant.id,
                conversation_id,
                run.spec.session_id,
                agent_id,
                agent_version,
                json.dumps([record.model_dump(mode="json")]),
                item_source_for_session_type(run.spec.session_type),
                json.dumps({"livekit_item_id": item_id, "data": {}}),
            )
            # `now()` is the transaction's, so it is the item's `created_at`.
            await conn.execute(
                """
                UPDATE conversations SET last_activity_at = GREATEST(last_activity_at, now())
                WHERE id = $1 AND tenant_id = $2
                """,
                conversation_id,
                tenant.id,
            )

    async def _handle(reader: rtc.ByteStreamReader, identity: str) -> None:
        stream_id = reader.info.stream_id
        if not can_see:
            # Only reachable from a client that is not our dashboard, which
            # hides the control — but that client exists. Drain first: an unread
            # stream leaves the sender's writer waiting on a reader.
            async for _ in reader:
                pass
            await _ack(identity, stream_id, "this agent's model cannot read images")
            return
        try:
            data = await _read(reader)
        except ImageRejected as exc:
            await _ack(identity, stream_id, str(exc))
            return

        # Held so a failure before the row is written can take the object back
        # out of the bucket. Cleared once the row exists: after that the object
        # is referenced, and deleting it would leave a transcript pointing at
        # nothing — a worse state than an orphan.
        orphan: ImageAttachment | None = None
        try:
            async with lock:
                pool = await db.tenant_pool(tenant)
                async with pool.acquire() as conn:
                    record, payload = await attachments.ingest_image(
                        conn,
                        tenant,
                        conversation_id,
                        data,
                        declared_mime=reader.info.mime_type or None,
                        filename=reader.info.name or None,
                    )
                orphan = record
                item_id = utils.shortuuid("item_")
                # Row first, then the context: it is the order in which every
                # failure state still matches what the ack about to be sent
                # says. The reverse leaves an image the model can answer about
                # under a "failed" thumbnail.
                await _persist(record, item_id)
                orphan = None
                # `update_chat_ctx`, never `generate_reply(user_input=…)`: that
                # commits the message only if its speech handle schedules, so an
                # interrupted or refused reply would drop the image out of the
                # context while our row says the caller sent one. This path is
                # unconditional and also pushes to a realtime session.
                agent = run.session.current_agent
                ctx = agent.chat_ctx.copy()
                ctx.insert(
                    llm.ChatMessage(
                        id=item_id,
                        role="user",
                        content=[
                            llm.ImageContent(
                                image=attachments.data_url(record.mime_type, payload),
                                inference_detail="auto",
                            )
                        ],
                    )
                )
                await agent.update_chat_ctx(ctx)
        except ImageRejected as exc:
            logger.error("image refused on session %s: %s", run.spec.session_id, exc)
            await _ack(identity, stream_id, str(exc))
            return
        except Exception:
            logger.exception("image failed on session %s", run.spec.session_id)
            if orphan is not None:
                await attachments.discard([orphan])
            await _ack(identity, stream_id, "that image could not be added to the call")
            return

        # And stop. No `generate_reply` — the caller's next turn picks the image
        # up, and the agent answers the question they were about to ask instead
        # of narrating a photo unprompted over the top of them.
        await _ack(identity, stream_id, None)

    def _on_stream(reader: rtc.ByteStreamReader, identity: str) -> None:
        task = asyncio.create_task(_handle(reader, identity), name="talqing_inbound_image")
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    room.register_byte_stream_handler(IMAGE_TOPIC, _on_stream)
