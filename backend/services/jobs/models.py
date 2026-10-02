"""What a scheduled job is, on the wire and in the table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class JobKind(StrEnum):
    """Every kind of deferred work, and the handler registry's keys.

    Dotted `resource.verb`, matching the webhook and session-event vocabularies
    — the same job name appears in a log line, a Kafka message and an operator's
    `UPDATE`, so it should read the same in all three.
    """

    SESSION_PURGE = "session.purge"
    """Delete one call's content: its recording, transcript, userdata and the
    personal data on its row. The metering an invoice is built from stays."""

    EMAIL_BATCH_DRAFT = "email.batch.draft"
    """Draft one email batch: claim a row, run the task on it, write its output
    onto the row, repeat. Long — one coroutine holds the batch from its first row
    to its last, sleeping in chunks between retries — and it completes only when
    the list is drafted or a human stops it. A drafted batch then holds no
    coroutine, no timer and no row in any queue."""

    EMAIL_SEND = "email.send"
    """Send the rows one human selected and pressed send on. Long, and
    deliberately: one coroutine holds the send until its queue is empty, which
    for a paced or daily-capped run is measured in days. It sleeps through a shut
    window, a spent cap and the gap between two emails, re-reading policy every
    `recheck_seconds` so a change reaches it without a restart."""

    WHATSAPP_SEND = "whatsapp.send"
    """Send one WhatsApp batch: one template per row, paced and daily-capped.
    Long, like `email.send` — one coroutine holds the batch until its last row
    has gone, sleeping through a shut window, a spent cap and the gap."""

    CALL_BATCH_DIAL = "call.batch.dial"
    """Take one dialling pass over a call batch: start it if its moment has come,
    settle the calls that ended, fill the free concurrency slots, and finish when
    there is nothing left to claim. Reschedules itself until then, so a batch
    between passes holds no coroutine, no lock and no timer — its row's
    `scheduled_at` is the whole of its schedule."""


@dataclass(frozen=True, slots=True)
class JobContext:
    """Which claim a handler is running under.

    A handler that works in one short pass never needs this. A handler that holds
    its job for hours does: "am I still the owner" is a question it has to ask on
    every claim, because a lease that lapsed under a container which then recovers
    leaves two copies of the loop looking at one subject. Both email handlers put
    these two values in the `WHERE` of every claim they make.
    """

    id: UUID
    """`scheduled_jobs.id` — the row this invocation holds."""

    worker: str
    """`executor._WORKER_ID` — who `claim` stamped into `claimed_by`."""


class ScheduledJob(BaseModel):
    """One due job, as Kafka carries it.

    Deliberately thin: the table holds the schedule and the outcome, and this
    carries only what the executor needs to find the row and run the work. Every
    field here is immutable for the life of the job, so a message that sat in a
    partition for a minute cannot describe a job differently from the row.
    """

    id: UUID
    tenant_id: UUID
    kind: JobKind
    args: dict[str, object] = {}

    @property
    def kafka_key(self) -> bytes:
        """Partition by the job itself, so its duplicates land together.

        At-least-once delivery and consumer-group rebalances both mean one job
        can be received twice; keying on its id puts both copies on one consumer
        where the claim turns the second into an in-order no-op. Keying on the
        tenant would serialize a workspace's whole backlog behind one partition
        for nothing — jobs of different subjects are independent.
        """
        return f"{self.tenant_id}:{self.id}".encode()
