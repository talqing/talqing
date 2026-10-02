"""background-worker: the process that runs scheduled jobs, and nothing else.

Every deferred thing this platform does is a `scheduled_jobs` row —
purging a call's content, drafting and sending an email batch, sending a
WhatsApp batch, taking one dialling pass over a call batch. `job-scheduler`
decides what is due and publishes it; the consumer here claims it with a
conditional `UPDATE` and runs it. **Kafka distributes; the claim decides.**

That is the whole process. There is no queue of its own, no lock, no sweep and
no cross-tenant poll, which is what makes `--scale background-worker=N` cost
nothing but money: two containers in one consumer group split the partitions
between them, and a job either of them receives twice is refused by the claim.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import db
from migrations.runner import migrate_data
from services.control import client as control
from services.jobs import executor as job_executor
from services.telephony import livekit_sip

logger = logging.getLogger("talqing.workers.background")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await migrate_data()

    logger.info("background worker up")
    try:
        await job_executor.consume(stop)
    finally:
        # Batch dialling reaches LiveKit through this shared client.
        await livekit_sip.aclose()
        # Every job resolves its tenant through the control plane.
        await control.aclose()
        await db.close_all()


if __name__ == "__main__":
    asyncio.run(main())
