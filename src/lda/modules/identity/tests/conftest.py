from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.identity.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.identity.infrastructure.persistence.unit_of_work import (
    SqlIdentityUnitOfWorkFactory,
)
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from platform_support import Database, make_wiring, provisioned_database

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ACTOR = PrincipalId(UUID(int=0xAC))
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def tenant_a() -> TenantId:
    return TENANT_A


@pytest.fixture
def tenant_b() -> TenantId:
    return TENANT_B


@pytest.fixture
def ctx_a() -> ExecutionContext:
    return TenantContext(TENANT_A, ACTOR, RequestId(UUID(int=1)))


@pytest.fixture
def ctx_b() -> ExecutionContext:
    return TenantContext(TENANT_B, ACTOR, RequestId(UUID(int=2)))


@pytest.fixture
def system_a() -> ExecutionContext:
    return SystemContext(TENANT_A, "bootstrap-first-principal")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def memory_factory() -> InMemoryIdentityUnitOfWorkFactory:
    return InMemoryIdentityUnitOfWorkFactory()


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_identity", "lda_app_identity", [MIGRATIONS_VERSIONS]) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE identity.principals"))
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
) -> SqlIdentityUnitOfWorkFactory:
    return SqlIdentityUnitOfWorkFactory(make_wiring(app_engine, clock, ids).factory)
