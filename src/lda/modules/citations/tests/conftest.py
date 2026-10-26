from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.citations.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.citations.infrastructure.persistence.unit_of_work import (
    UnitOfWorkGenerationRecords,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database

START = __import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").UTC)


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_citations", "lda_app_citations", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """The owner/superuser oracle. Cleanup uses replica mode (superuser-only) to bypass the
    insert-only triggers; the runtime role can never do this."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM citations.generation_record_citations"))
        conn.execute(text("DELETE FROM citations.generation_records"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sql_records(app_engine: Engine) -> UnitOfWorkGenerationRecords:
    wiring = make_wiring(app_engine, FakeClock(START), SequentialIdGenerator())
    return UnitOfWorkGenerationRecords(wiring.factory)
