from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.platform.testing.doubles import FakeClock, InMemoryAuditRecorder, SequentialIdGenerator
from platform_support import Database, provisioned_database


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database() as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def audit() -> InMemoryAuditRecorder:
    return InMemoryAuditRecorder()


@pytest.fixture
def database_url(database: Database, admin_engine: Engine) -> str:
    """Runtime-role URL (after the table has been truncated) for tests needing a custom engine."""
    return database.app_url
