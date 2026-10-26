from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.audit.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from platform_support import Database, provisioned_database

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
ACTOR = PrincipalId(UUID(int=0xAC))
REQUEST = RequestId(UUID(int=0x11))
WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def a_fact(**over: object) -> AuditFact:
    """A valid fact; override any field to build an invalid or different one."""
    fields: dict[str, object] = {
        "tenant_id": TENANT_A,
        "actor": ACTOR,
        "action": "identity.principal_disabled",
        "resource_type": "principal",
        "resource_id": "0f6c1a52-0000-4000-8000-000000000001",
        "outcome": AuditOutcome.SUCCESS,
        "request_id": REQUEST,
        "occurred_at": WHEN,
        "attributes": {"principal_kind": "user"},
        **over,
    }
    return AuditFact(**fields)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(WHEN)


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_audit", "lda_app_audit", [MIGRATIONS_VERSIONS]) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """The table owner/superuser: the tests' oracle. Cleanup bypasses the insert-only triggers by
    switching the session to replica mode, which only a superuser can do (never the runtime)."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM audit.records"))
        conn.execute(text("DELETE FROM platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()
