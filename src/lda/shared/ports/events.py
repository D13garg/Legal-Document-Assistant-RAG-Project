from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from lda.shared.kernel.events import DomainEvent


class Delivery(StrEnum):
    SYNC_IN_TXN = (
        "sync_in_txn"  # handlers run inside the publisher's transaction; failure aborts it
    )
    ASYNC_JOB = "async_job"  # one idempotent job per handler, enqueued in the same transaction


class EventPublisher(Protocol):
    def publish(self, event: DomainEvent, delivery: Delivery) -> None: ...
