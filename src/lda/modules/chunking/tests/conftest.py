from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.chunking.application.ports import ChunkingUnitOfWorkFactory
from lda.modules.chunking.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.chunking.infrastructure.persistence.unit_of_work import (
    SqlChunkingUnitOfWorkFactory,
)
from lda.modules.chunking.testing import InMemoryChunkingUnitOfWorkFactory
from lda.modules.chunking.tests.support import START, Rig, build_rig
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database


@pytest.fixture
def rig() -> Rig:
    return build_rig()


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_chunking", "lda_app_chunking", [MIGRATIONS_VERSIONS]) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """The owner/superuser oracle. Cleanup uses replica mode (superuser-only) to bypass the
    immutability and insert-only triggers; the runtime role can never do this."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM chunking.purge_receipts"))
        conn.execute(text("DELETE FROM chunking.chunk_sets"))
        conn.execute(text("DELETE FROM platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def memory_factory() -> InMemoryChunkingUnitOfWorkFactory:
    return InMemoryChunkingUnitOfWorkFactory()


@pytest.fixture
def sql_factory(app_engine: Engine) -> SqlChunkingUnitOfWorkFactory:
    wiring = make_wiring(app_engine, FakeClock(START), SequentialIdGenerator())
    return SqlChunkingUnitOfWorkFactory(wiring.factory)


@pytest.fixture(params=["memory", "sql"])
def factory(request: pytest.FixtureRequest) -> ChunkingUnitOfWorkFactory:
    name = "memory_factory" if request.param == "memory" else "sql_factory"
    value: ChunkingUnitOfWorkFactory = request.getfixturevalue(name)
    return value
