"""Event codec strictness, async handler job semantics and publisher idempotency (ADR-028)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

import pytest
from sqlalchemy import Engine

from lda.platform.events.codec import EventCodec
from lda.platform.events.job_handler import EventJobHandler
from lda.platform.events.registry import EVENT_QUEUE
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import (
    ConflictError,
    DomainError,
    InfrastructureError,
    ValidationError,
)
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId, JobId, PrincipalId
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import ClaimedJob, Failed, JobSpec, Succeeded
from lda.shared.ports.unit_of_work import UnitOfWork
from platform_support import Wiring, job_rows, make_wiring, tenant

NOW = datetime(2026, 1, 1, tzinfo=UTC)
_THING = UUID(int=5)


class Color(StrEnum):
    RED = "red"


@dataclass(frozen=True, kw_only=True)
class Painted(DomainEvent):
    type_name: ClassVar[str] = "tests.Painted"
    count: int
    ratio: float
    flag: bool
    label: str | None = None
    color: Color = Color.RED
    by: PrincipalId | None = None
    thing: UUID = field(default=_THING)


@dataclass(frozen=True, kw_only=True)
class Other(DomainEvent):
    type_name: ClassVar[str] = "tests.Other"


def painted(n: int = 1, key: int = 1, **kw: object) -> Painted:
    base: dict[str, object] = {"count": 3, "ratio": 0.5, "flag": True}
    return Painted(
        event_id=EventId(UUID(int=key)),
        tenant_id=tenant(n),
        occurred_at=NOW,
        **{**base, **kw},  # type: ignore[arg-type]
    )


@pytest.fixture
def codec() -> EventCodec:
    c = EventCodec()
    c.register(Painted)
    return c


# ---- codec ---------------------------------------------------------------------------------------


def test_round_trip_preserves_every_supported_type_and_is_plain_json(codec: EventCodec) -> None:
    event = painted(label="x", by=PrincipalId(UUID(int=9)))
    payload = codec.encode(event)
    assert codec.decode(json.loads(json.dumps(payload))) == event
    assert set(payload) == {"type", "data"}


@pytest.mark.parametrize(
    "patch",
    [
        {"count": "3"},  # str for int
        {"count": True},  # bool is not an int
        {"count": 3.5},
        {"ratio": "0.5"},
        {"ratio": True},
        {"flag": 1},
        {"label": 123},
        {"label": ["x"]},
        {"color": "blue"},
        {"thing": "not-a-uuid"},
        {"occurred_at": "2026-01-01T00:00:00"},  # naive datetime
        {"occurred_at": "garbage"},
        {"tenant_id": "garbage"},
    ],
)
def test_wrongly_typed_or_corrupt_payloads_are_rejected(
    codec: EventCodec, patch: dict[str, object]
) -> None:
    payload = json.loads(json.dumps(codec.encode(painted())))
    payload["data"].update(patch)
    with pytest.raises(ValidationError, match="event_decode_failed"):
        codec.decode(payload)


def test_integer_json_numbers_are_valid_floats(codec: EventCodec) -> None:
    payload = json.loads(json.dumps(codec.encode(painted())))
    payload["data"]["ratio"] = 1
    assert codec.decode(payload).ratio == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"type": "tests.Painted"},
        {"type": "tests.Painted", "data": []},
        {"type": 5, "data": {}},
        {"type": "nope", "data": {}},
    ],
)
def test_structurally_invalid_payloads_are_rejected(
    codec: EventCodec, payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="event_decode_failed"):
        codec.decode(payload)  # type: ignore[arg-type]


def test_missing_fields_are_rejected(codec: EventCodec) -> None:
    payload = json.loads(json.dumps(codec.encode(painted())))
    del payload["data"]["count"]
    with pytest.raises(ValidationError, match="event_decode_failed"):
        codec.decode(payload)


def test_unregistered_events_cannot_be_encoded(codec: EventCodec) -> None:
    with pytest.raises(ValidationError, match="event_not_registered"):
        codec.encode(Other(event_id=EventId(UUID(int=1)), tenant_id=tenant(1), occurred_at=NOW))


def test_registration_rejects_bad_definitions() -> None:
    @dataclass(frozen=True, kw_only=True)
    class NoName(DomainEvent):
        pass

    @dataclass(frozen=True, kw_only=True)
    class ListField(DomainEvent):
        type_name: ClassVar[str] = "tests.ListField"
        items: list[str]

    @dataclass(frozen=True, kw_only=True)
    class WideUnion(DomainEvent):
        type_name: ClassVar[str] = "tests.WideUnion"
        value: int | str

    @dataclass(frozen=True, kw_only=True)
    class Duplicate(DomainEvent):
        type_name: ClassVar[str] = "tests.Painted"

    c = EventCodec()
    c.register(Painted)
    for bad in (NoName, ListField, WideUnion, Duplicate):
        with pytest.raises(ValueError):
            c.register(bad)


# ---- EventJobHandler -----------------------------------------------------------------------------


class Probe:
    name = "probe"

    def __init__(self, fail: bool = False) -> None:
        self.seen: list[tuple[DomainEvent, object]] = []
        self.fail = fail

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        self.seen.append((event, uow.tenant_id))
        uow.jobs.enqueue(
            JobSpec(queue="side", job_type="side_effect", unique_key=f"se:{event.event_id}")
        )
        if self.fail:
            raise InfrastructureError("handler_failed")


@pytest.fixture
def wiring(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, codec: EventCodec
) -> Wiring:
    w = make_wiring(app_engine, clock, ids)
    w.codec.register(Painted)
    return w


def claimed(w: Wiring, event: DomainEvent, job_tenant: int = 1) -> ClaimedJob:
    return ClaimedJob(
        id=JobId(UUID(int=77)),
        tenant_id=tenant(job_tenant),
        queue=EVENT_QUEUE,
        job_type="event:probe",
        payload=w.codec.encode(event),
        attempt=1,
        max_attempts=3,
        fencing_token=1,
    )


def ctx(n: int = 1) -> SystemContext:
    return SystemContext(tenant(n), "job:event:probe", JobId(UUID(int=77)))


def test_handler_runs_in_a_tenant_bound_transaction_and_commits(
    wiring: Wiring, admin_engine: Engine
) -> None:
    probe = Probe()
    outcome = EventJobHandler(probe, wiring.codec, wiring.factory).handle(
        claimed(wiring, painted()), ctx()
    )
    assert outcome == Succeeded() and probe.seen[0][1] == tenant(1)
    assert [r.tenant_id for r in job_rows(admin_engine)] == [tenant(1).value]


def test_redelivery_is_safe_when_the_handler_is_idempotent(
    wiring: Wiring, admin_engine: Engine
) -> None:
    probe, handler = Probe(), None
    handler = EventJobHandler(probe, wiring.codec, wiring.factory)
    for _ in range(3):  # at-least-once: the same job may run again
        assert handler.handle(claimed(wiring, painted()), ctx()) == Succeeded()
    assert len(probe.seen) == 3 and len(job_rows(admin_engine)) == 1  # side effect deduplicated


def test_undecodable_payload_dead_letters_without_running_the_handler(wiring: Wiring) -> None:
    probe = Probe()
    job = claimed(wiring, painted())
    bad = ClaimedJob(**{**job.__dict__, "payload": {"type": "nope", "data": {}}})
    assert EventJobHandler(probe, wiring.codec, wiring.factory).handle(bad, ctx()) == Failed(
        error_code="event_decode_failed"
    )
    assert probe.seen == []


def test_event_for_a_different_tenant_than_the_job_is_refused(wiring: Wiring) -> None:
    probe = Probe()
    job = claimed(
        wiring, painted(n=2), job_tenant=1
    )  # payload says tenant 2, job row says tenant 1
    outcome = EventJobHandler(probe, wiring.codec, wiring.factory).handle(job, ctx(1))
    assert outcome == Failed(error_code="event_tenant_mismatch") and probe.seen == []


def test_handler_failure_rolls_back_its_effects_and_propagates_for_retry(
    wiring: Wiring, admin_engine: Engine
) -> None:
    with pytest.raises(InfrastructureError, match="handler_failed"):
        EventJobHandler(Probe(fail=True), wiring.codec, wiring.factory).handle(
            claimed(wiring, painted()), ctx()
        )
    assert job_rows(admin_engine) == []


# ---- publisher -----------------------------------------------------------------------------------


class Named:
    def __init__(self, name: str) -> None:
        self.name = name

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        raise AssertionError("async handlers never run in the publisher")


def publish_twice(w: Wiring, events: list[Painted]) -> None:
    for event in events:
        with w.factory.begin(SystemContext(event.tenant_id, "t")) as uow:
            uow.events.publish(event, Delivery.ASYNC_JOB)
            uow.commit()


def test_async_publish_creates_one_job_per_handler_and_event(
    wiring: Wiring, admin_engine: Engine
) -> None:
    wiring.registry.register(Painted, Named("a"), Delivery.ASYNC_JOB)
    wiring.registry.register(Painted, Named("b"), Delivery.ASYNC_JOB)
    publish_twice(wiring, [painted(key=1), painted(key=1), painted(key=2)])
    rows = job_rows(admin_engine)
    assert len(rows) == 4  # 2 handlers x 2 distinct events; the repeat of event 1 added nothing
    assert all(r.payload["type"] == "tests.Painted" for r in rows)


def test_async_jobs_are_not_created_when_the_publishing_transaction_rolls_back(
    wiring: Wiring, admin_engine: Engine
) -> None:
    wiring.registry.register(Painted, Named("a"), Delivery.ASYNC_JOB)
    with pytest.raises(RuntimeError), wiring.factory.begin(SystemContext(tenant(1), "t")) as uow:
        uow.events.publish(painted(), Delivery.ASYNC_JOB)
        raise RuntimeError("use case failed")
    assert job_rows(admin_engine) == []


def test_sync_delivery_only_runs_sync_handlers_for_the_exact_type(wiring: Wiring) -> None:
    sync, other_type, asynchronous = Probe(), Probe(), Named("async-one")
    other_type.name = "other-type"
    wiring.registry.register(Painted, sync, Delivery.SYNC_IN_TXN)
    wiring.registry.register(Other, other_type, Delivery.SYNC_IN_TXN)
    wiring.registry.register(Painted, asynchronous, Delivery.ASYNC_JOB)
    with wiring.factory.begin(SystemContext(tenant(1), "t")) as uow:
        uow.events.publish(painted(), Delivery.SYNC_IN_TXN)
        uow.commit()
    assert len(sync.seen) == 1 and other_type.seen == []


def test_cross_tenant_publish_is_rejected_even_from_a_system_context(wiring: Wiring) -> None:
    with (
        pytest.raises(DomainError, match="event_tenant_mismatch"),
        wiring.factory.begin(SystemContext(tenant(1), "t")) as uow,
    ):
        uow.events.publish(painted(n=2), Delivery.SYNC_IN_TXN)


def test_registry_rejects_duplicate_handler_names_per_delivery(wiring: Wiring) -> None:
    wiring.registry.register(Painted, Named("a"), Delivery.ASYNC_JOB)
    with pytest.raises(ConflictError, match="handler_already_registered"):
        wiring.registry.register(Other, Named("a"), Delivery.ASYNC_JOB)
