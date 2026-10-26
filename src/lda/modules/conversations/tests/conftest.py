from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine

from lda.modules.conversations.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.conversations.infrastructure.persistence.unit_of_work import (
    SqlConversationsUnitOfWorkFactory,
)
from lda.modules.conversations.testing import InMemoryConversationsUnitOfWorkFactory
from lda.modules.conversations.tests.support import Kit, build_kit
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_conversations", "lda_app_conversations", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sql_factory(app_engine: Engine) -> SqlConversationsUnitOfWorkFactory:
    wiring = make_wiring(app_engine, FakeClock(), SequentialIdGenerator())
    return SqlConversationsUnitOfWorkFactory(wiring.factory)


@pytest.fixture(params=["memory", "sql"])
def kit(request: pytest.FixtureRequest) -> Kit:
    """The same scenario on the in-memory twin and on real PostgreSQL. Each test uses a fresh
    random tenant, so no cleanup is needed (the schema's triggers refuse TRUNCATE by design)."""
    if request.param == "memory":
        return build_kit(InMemoryConversationsUnitOfWorkFactory())
    return build_kit(request.getfixturevalue("sql_factory"))


@pytest.fixture
def sql_kit(sql_factory: SqlConversationsUnitOfWorkFactory) -> Kit:
    """For assertions that only a database can make (no skipped parametrization)."""
    return build_kit(sql_factory)
