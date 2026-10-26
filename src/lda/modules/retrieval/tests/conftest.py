from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.retrieval.application.ports import RetrievalUnitOfWorkFactory
from lda.modules.retrieval.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.retrieval.infrastructure.persistence.unit_of_work import (
    SqlRetrievalUnitOfWorkFactory,
)
from lda.modules.retrieval.testing import InMemoryRetrievalUnitOfWorkFactory
from lda.modules.retrieval.tests.support import START, Rig, build_rig
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database


@pytest.fixture
def rig() -> Rig:
    return build_rig()


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_retrieval", "lda_app_retrieval", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """Owner/superuser oracle; cleanup uses replica mode to bypass immutability triggers."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM retrieval.purge_receipts"))
        conn.execute(text("DELETE FROM retrieval.chunk_vectors"))
        conn.execute(text("DELETE FROM retrieval.index_versions"))
        conn.execute(text("DELETE FROM platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def memory_factory() -> InMemoryRetrievalUnitOfWorkFactory:
    return InMemoryRetrievalUnitOfWorkFactory()


@pytest.fixture
def sql_factory(app_engine: Engine) -> SqlRetrievalUnitOfWorkFactory:
    wiring = make_wiring(app_engine, FakeClock(START), SequentialIdGenerator())
    return SqlRetrievalUnitOfWorkFactory(wiring.factory)


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> RetrievalUnitOfWorkFactory:
    name = "memory_factory" if request.param == "memory" else "sql_factory"
    value: RetrievalUnitOfWorkFactory = request.getfixturevalue(name)
    return value
