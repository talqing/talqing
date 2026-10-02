"""Kafka transport for due scheduled jobs.

Mirrors `services.messaging.textq` deliberately — a pydantic job model, one
module-level producer behind a lock, `acks="all"` — because two Kafka producers
in one codebase that disagree about durability is a bug waiting for the first
broker restart.

Kafka carries only "this job is due now". The schedule lives in `scheduled_jobs`
and nothing is ever published ahead of time with a delay.
"""

from __future__ import annotations

import asyncio

from aiokafka import AIOKafkaProducer

from settings import get_settings

from .models import ScheduledJob

_producer: AIOKafkaProducer | None = None
_producer_lock = asyncio.Lock()


async def _get_producer() -> AIOKafkaProducer:
    global _producer
    if _producer is None:
        async with _producer_lock:
            if _producer is None:
                settings = get_settings()
                producer = AIOKafkaProducer(
                    bootstrap_servers=settings.kafka.bootstrap_servers,
                    acks="all",
                    enable_idempotence=True,
                )
                await producer.start()
                _producer = producer
    return _producer


async def publish_job(job: ScheduledJob) -> None:
    producer = await _get_producer()
    await producer.send_and_wait(
        get_settings().kafka.jobs_topic,
        key=job.kafka_key,
        value=job.model_dump_json().encode(),
    )


async def close() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
