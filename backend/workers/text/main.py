"""Kafka consumer process for interruptible text agents."""

from __future__ import annotations

import asyncio
import logging
import signal
from uuid import UUID

from aiokafka import AIOKafkaConsumer

import db
from migrations.runner import migrate_data
from services import catalog
from services.control import client as control
from services.messaging import TextTurnJob, textq
from services.user import load_tenant
from settings import get_settings
from workers.text.actor import ConversationActor

logger = logging.getLogger("talqing.workers.text")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = get_settings()
    await migrate_data()
    # A text window compiles its agent in THIS process, and validation for a
    # `run_task` on a draft happens here too, so this worker holds the
    # searched-provider model registry the way the API does.
    registry = catalog.start_model_registry()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    consumer = AIOKafkaConsumer(
        settings.kafka.text_turns_topic,
        bootstrap_servers=settings.kafka.bootstrap_servers,
        group_id=settings.kafka.text_consumer_group,
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    actors: dict[tuple[UUID, UUID], ConversationActor] = {}
    await consumer.start()
    try:
        logger.info(
            "text worker ready on %s (customer text + CoPilots)",
            settings.kafka.text_turns_topic,
        )
        while not stop.is_set():
            batch = await consumer.getmany(timeout_ms=1000, max_records=500)
            for records in batch.values():
                for record in records:
                    try:
                        job = TextTurnJob.model_validate_json(record.value)
                    except Exception:
                        logger.exception("dropping malformed text-turn Kafka event")
                        continue
                    key = (job.tenant_id, job.conversation_id)
                    actor = actors.get(key)
                    if actor is None:
                        tenant = await load_tenant(job.tenant_id)
                        if tenant is None:
                            logger.error(
                                "dropping text input %s for missing tenant %s",
                                job.input_item_id,
                                job.tenant_id,
                            )
                            continue
                        actor = ConversationActor(tenant, job.conversation_id)
                        actors[key] = actor
                    await actor.submit(job)

            for key, actor in list(actors.items()):
                if actor.idle:
                    actors.pop(key, None)
    finally:
        if registry is not None:
            registry.cancel()
        await consumer.stop()
        await asyncio.gather(
            *(actor.aclose() for actor in actors.values()),
            return_exceptions=True,
        )
        await textq.close()
        # A warm ConversationActor is started by resolving its tenant through
        # the control plane; this is the client that did it.
        await control.aclose()
        await db.close_all()


if __name__ == "__main__":
    asyncio.run(main())
