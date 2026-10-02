"""job-scheduler: the process that notices work has come due.

It does nothing else, and that is the point. Finding due work, giving it to
exactly one worker and letting a dead container's work be picked up again used to
be solved twice — bespoke in the batch dispatcher, with a Redis lock and a
lock-TTL argument, and generically in `scheduled_jobs`. Only the generic answer
is left, and this container exists so it has somewhere to live that is not
"another loop inside background-worker".

It never runs a job. It publishes "this is due now" to Kafka and the
`background-worker` executor claims it — so this can safely run as a single
replica, and running two would cost duplicate messages the claim already
absorbs.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import db
from migrations.runner import migrate_data
from services.jobs import queue, scheduler

logger = logging.getLogger("talqing.workers.scheduler")


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
    logger.info("job scheduler up (tick=%.0fs)", scheduler.TICK_SECONDS)
    try:
        await scheduler.discovery_loop(stop)
    finally:
        await queue.close()
        await db.close_all()


if __name__ == "__main__":
    asyncio.run(main())
