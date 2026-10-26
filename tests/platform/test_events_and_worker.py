from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from lda.platform.configuration.settings import JobWorkerSettings
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.unit_of_work import SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.testing.doubles import FakeClock, InMemoryAuditRecorder, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import DomainError, InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import ClaimedJob, Failed, JobOutcome, JobSpec, RetryAfter, Succeeded
from lda.shared.ports.unit_of_work import UnitOfWork
from platform_support import tenant


@dataclass(frozen=True, kw_only=True)
class ThingHappened(DomainEvent):
    type_name: ClassVar[str] = "tests.ThingHappened"
    thing_id: UUID
    note: str | None = None


class Recorder:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name, self.fail, self.seen = name, fail, []  # type: ignore[var-annotated]

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        self.seen.append(event)
        if self.fail:
            raise InfrastructureError("handler_failed")
        uow.audit.record(
            AuditFact(
                tenant_id=uow.tenant_id,
                actor=None,
                action="thing.seen",
                resource_type="thing",
                resource_id=None,
                outcome=AuditOutcome.SUCCESS,
                request_id=None,
                occurred_at=event.occurred_at,
            )
        )


def event(n: int = 1, key: int = 1) -> ThingHappened:
    return ThingHappened(
        event_id=EventId(UUID(int=key)),
        tenant_id=tenant(n),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        thing_id=UUID(int=9),
        note="n",
    )


@pytest.fixture
def wiring(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, audit: InMemoryAuditRecorder
):
    registry, codec = HandlerRegistry(), EventCodec()
    codec.register(ThingHappened)
    factory = SqlUnitOfWorkFactory(
        app_engine,
        clock,
        ids,
        lambda tx: audit,
        lambda uow, jobs: TransactionalEventPublisher(registry, codec, jobs, uow),
    )
    return factory, registry, codec


def test_codec_roundtrip_and_rejects_unregistered(wiring) -> None:
    _, _, codec = wiring
    assert codec.decode(codec.encode(event())) == event()
    with pytest.raises(Exception, match="event_decode_failed"):
        codec.decode({"type": "nope", "data": {}})


def test_sync_handlers_run_in_the_transaction_and_failure_aborts_it(
    wiring, admin_engine: Engine
) -> None:
    factory, registry, _ = wiring
    registry.register(ThingHappened, Recorder("a", fail=True), Delivery.SYNC_IN_TXN)
    with pytest.raises(InfrastructureError), factory.begin(SystemContext(tenant(1), "t")) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", job_type="should_roll_back"))
        uow.events.publish(event(), Delivery.SYNC_IN_TXN)
        uow.commit()
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one() == 0


def test_publishing_for_another_tenant_is_rejected(wiring) -> None:
    factory, _, _ = wiring
    with (
        pytest.raises(DomainError, match="event_tenant_mismatch"),
        factory.begin(SystemContext(tenant(1), "t")) as uow,
    ):
        uow.events.publish(event(n=2), Delivery.ASYNC_JOB)


def test_async_delivery_is_idempotent_and_executed_by_the_worker(
    wiring, app_engine: Engine, clock: FakeClock, admin_engine: Engine, audit: InMemoryAuditRecorder
) -> None:
    factory, registry, codec = wiring
    handler = Recorder("projector")
    registry.register(ThingHappened, handler, Delivery.ASYNC_JOB)
    for _ in range(2):  # same event published twice -> one job
        with factory.begin(SystemContext(tenant(1), "t")) as uow:
            uow.events.publish(event(), Delivery.ASYNC_JOB)
            uow.commit()
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM platform.jobs")).scalar_one() == 1
    settings = JobWorkerSettings(queues={"events": 2})
    worker = JobWorker(
        JobLeaseStore(app_engine, clock),
        {"event:projector": EventJobHandler(handler, codec, factory)},
        settings,
        clock,
        "w1",
        BackoffPolicy(1, 10, lambda: 0.0),
    )
    assert worker.run_once() == 1 and worker.run_once() == 0
    assert len(handler.seen) == 1 and len(audit.facts) == 1
    with admin_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM platform.jobs")).scalar_one() == "succeeded"


class Scripted:
    def __init__(self, outcomes: list[JobOutcome | Exception]) -> None:
        self.outcomes = outcomes

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        assert ctx.tenant_id == job.tenant_id and ctx.job_id == job.id
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run_worker(app_engine: Engine, clock: FakeClock, handlers: dict[str, Scripted]) -> JobWorker:
    return JobWorker(
        JobLeaseStore(app_engine, clock),
        handlers,
        JobWorkerSettings(queues={"q": 5}),
        clock,
        "w",
        BackoffPolicy(10, 100, lambda: 1.0),
    )


def enqueue(factory: SqlUnitOfWorkFactory, **kw: object) -> None:
    with factory.begin(SystemContext(tenant(1), "t")) as uow:
        uow.jobs.enqueue(JobSpec(queue="q", **kw))  # type: ignore[arg-type]
        uow.commit()


def status(admin: Engine) -> tuple[str, str | None]:
    with admin.connect() as conn:
        r = conn.execute(text("SELECT status, last_error_code FROM platform.jobs")).one()
    return r.status, r.last_error_code


def test_unhandled_exception_retries_with_backoff_then_succeeds(
    wiring, app_engine, clock, admin_engine
) -> None:
    factory, _, _ = wiring
    enqueue(factory, job_type="t", max_attempts=3)
    worker = run_worker(
        app_engine, clock, {"t": Scripted([RuntimeError("secret detail"), Succeeded()])}
    )
    worker.run_once()
    assert status(admin_engine) == ("queued", "unhandled_exception")  # error text never stored
    assert worker.run_once() == 0  # backoff not elapsed
    clock.advance(timedelta(seconds=11))
    worker.run_once()
    assert status(admin_engine)[0] == "succeeded"


def test_permanent_failure_and_missing_handler_dead_letter(
    wiring, app_engine, clock, admin_engine
) -> None:
    factory, _, _ = wiring
    enqueue(factory, job_type="unknown")
    run_worker(app_engine, clock, {}).run_once()
    assert status(admin_engine) == ("dead", "no_handler")


def test_handler_can_request_retry_without_consuming_attempts(
    wiring, app_engine, clock, admin_engine
) -> None:
    factory, _, _ = wiring
    enqueue(factory, job_type="t", max_attempts=1)
    worker = run_worker(
        app_engine,
        clock,
        {
            "t": Scripted(
                [
                    RetryAfter(
                        delay=timedelta(seconds=1),
                        error_code="provider_unavailable",
                        consumes_attempt=False,
                    ),
                    Failed(error_code="bad"),
                ]
            )
        },
    )
    worker.run_once()
    assert status(admin_engine) == ("queued", "provider_unavailable")
    clock.advance(timedelta(seconds=2))
    worker.run_once()
    assert status(admin_engine) == ("dead", "bad")


def test_run_until_stops_on_event(wiring, app_engine, clock) -> None:
    worker = run_worker(app_engine, clock, {})
    stop = threading.Event()
    stop.set()
    worker.run_until(stop)  # returns immediately; must not hang
