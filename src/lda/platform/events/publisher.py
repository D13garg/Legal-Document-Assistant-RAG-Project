from __future__ import annotations

from lda.platform.events.codec import EventCodec
from lda.platform.events.registry import EVENT_QUEUE, HandlerRegistry
from lda.shared.kernel.errors import DomainError
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobQueue, JobSpec
from lda.shared.ports.unit_of_work import UnitOfWork


class TransactionalEventPublisher:
    """SYNC_IN_TXN: run handlers now, inside the transaction (a failure aborts it).
    ASYNC_JOB: enqueue one job per handler in the same transaction, unique on (handler, event)."""

    def __init__(
        self, registry: HandlerRegistry, codec: EventCodec, jobs: JobQueue, uow: UnitOfWork
    ) -> None:
        self._registry = registry
        self._codec = codec
        self._jobs = jobs
        self._uow = uow

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        if event.tenant_id != self._uow.tenant_id:
            raise DomainError("event_tenant_mismatch")
        handlers = self._registry.handlers_for(type(event), delivery)
        if delivery is Delivery.SYNC_IN_TXN:
            for handler in handlers:
                handler.handle(event, self._uow)
            return
        for handler in handlers:
            self._jobs.enqueue(
                JobSpec(
                    queue=EVENT_QUEUE,
                    job_type=f"event:{handler.name}",
                    payload=self._codec.encode(event),
                    unique_key=f"event:{handler.name}:{event.event_id}",
                )
            )
