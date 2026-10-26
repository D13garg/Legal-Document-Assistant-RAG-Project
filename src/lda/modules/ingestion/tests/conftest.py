from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.ingestion.domain.events import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.modules.ingestion.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.ingestion.infrastructure.persistence.unit_of_work import (
    SqlIngestionUnitOfWorkFactory,
)
from lda.modules.ingestion.testing import (
    InMemoryIngestionStore,
    InMemoryIngestionUnitOfWorkFactory,
)
from lda.modules.ingestion.tests.support import START, Rig, build_rig
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.events import DomainEvent
from lda.shared.ports.events import Delivery
from lda.shared.ports.unit_of_work import UnitOfWork
from platform_support import Database, make_wiring, provisioned_database


class OutcomeSink:
    """Stands in for Documents (which ingestion may not import): its job proves that an outcome
    event was durably enqueued in the same transaction as the run's state."""

    def __init__(self, name: str) -> None:
        self.name = name

    def handle(self, event: DomainEvent, uow: UnitOfWork) -> None:
        return None


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def memory_store() -> InMemoryIngestionStore:
    return InMemoryIngestionStore()


@pytest.fixture
def memory_factory(memory_store: InMemoryIngestionStore) -> InMemoryIngestionUnitOfWorkFactory:
    return InMemoryIngestionUnitOfWorkFactory(memory_store)


@pytest.fixture
def rig(
    memory_factory: InMemoryIngestionUnitOfWorkFactory, clock: FakeClock, ids: SequentialIdGenerator
) -> Rig:
    return build_rig(memory_factory, clock, ids)


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_ingestion", "lda_app_ingestion", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """The owner/superuser oracle (replica mode skips the guard triggers; the app role can't)."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM ingestion.stage_runs"))
        conn.execute(text("DELETE FROM ingestion.runs"))
        conn.execute(text("DELETE FROM platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sql_factory(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> SqlIngestionUnitOfWorkFactory:
    wiring = make_wiring(app_engine, clock, ids)
    for event_type in (IngestionCompleted, IngestionFailed, IngestionQuarantined):
        wiring.codec.register(event_type)
        wiring.registry.register(
            event_type, OutcomeSink(f"sink_{event_type.__name__}"), Delivery.ASYNC_JOB
        )
    return SqlIngestionUnitOfWorkFactory(wiring.factory)
