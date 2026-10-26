from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.access_control.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.access_control.infrastructure.persistence.unit_of_work import (
    SqlAccessControlUnitOfWorkFactory,
)
from lda.modules.access_control.testing import (
    InMemoryAccessControlUnitOfWorkFactory,
)
from lda.modules.access_control.tests.support import START, Rig, build_rig
from lda.modules.identity.testing import InMemoryIdentityUnitOfWorkFactory
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def memory_policy_factory() -> InMemoryAccessControlUnitOfWorkFactory:
    return InMemoryAccessControlUnitOfWorkFactory()


@pytest.fixture
def rig(memory_policy_factory: InMemoryAccessControlUnitOfWorkFactory, clock: FakeClock) -> Rig:
    """ALICE and BOB are active in tenant A; ALICE (another principal row) is active in B."""
    return build_rig(InMemoryIdentityUnitOfWorkFactory(), memory_policy_factory, clock)


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_access_control", "lda_app_access_control", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE access_control.grants, access_control.policy_epochs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sql_policy_factory(app_engine: Engine, clock: FakeClock) -> SqlAccessControlUnitOfWorkFactory:
    return SqlAccessControlUnitOfWorkFactory(
        make_wiring(app_engine, clock, SequentialIdGenerator()).factory
    )


@pytest.fixture
def sql_rig(sql_policy_factory: SqlAccessControlUnitOfWorkFactory, clock: FakeClock) -> Rig:
    """Real Postgres policy storage; Identity stays in memory (its SQL adapter is private)."""
    return build_rig(InMemoryIdentityUnitOfWorkFactory(), sql_policy_factory, clock)


@pytest.fixture(params=["memory", "sql"])
def any_rig(request: pytest.FixtureRequest) -> Rig:
    """The same behaviour must hold on the in-memory and the real-Postgres policy storage."""
    name = "rig" if request.param == "memory" else "sql_rig"
    rig: Rig = request.getfixturevalue(name)
    return rig
