from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.documents.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.documents.infrastructure.persistence.unit_of_work import (
    SqlDocumentsUnitOfWorkFactory,
)
from lda.modules.documents.testing import (
    InMemoryDocumentsStore,
    InMemoryDocumentsUnitOfWorkFactory,
)
from lda.modules.documents.tests.support import START, Rig, build_rig
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore, SequentialIdGenerator
from platform_support import Database, make_wiring, provisioned_database


class CountingRevoker:
    """Stands in for Access Control (a module Documents may not import): counts what it is asked."""

    def __init__(self, calls: list[tuple[str, str]], result: int) -> None:
        self._calls = calls
        self._result = result

    def revoke_all(self, resource_kind: str, resource_id: str) -> int:
        self._calls.append((resource_kind, resource_id))
        return self._result


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def ids() -> SequentialIdGenerator:
    return SequentialIdGenerator()


@pytest.fixture
def objects() -> InMemoryObjectStore:
    return InMemoryObjectStore()


@pytest.fixture
def memory_store() -> InMemoryDocumentsStore:
    return InMemoryDocumentsStore()


@pytest.fixture
def memory_factory(memory_store: InMemoryDocumentsStore) -> InMemoryDocumentsUnitOfWorkFactory:
    return InMemoryDocumentsUnitOfWorkFactory(memory_store)


@pytest.fixture
def rig(
    memory_factory: InMemoryDocumentsUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    objects: InMemoryObjectStore,
) -> Rig:
    return build_rig(memory_factory, clock, ids, objects)


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_documents", "lda_app_documents", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """The owner/superuser oracle. Cleanup uses replica mode (superuser-only) to bypass the
    insert-only and purge-only triggers; the runtime role can never do this."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM documents.purge_receipts"))
        conn.execute(text("DELETE FROM documents.upload_intents"))
        conn.execute(text("DELETE FROM documents.versions"))
        conn.execute(text("DELETE FROM documents.documents"))
        conn.execute(text("DELETE FROM platform.jobs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url)
    yield engine
    engine.dispose()


@pytest.fixture
def revoker_calls() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def sql_factory(
    app_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    revoker_calls: list[tuple[str, str]],
) -> SqlDocumentsUnitOfWorkFactory:
    def revokers(tx: TenantTransaction) -> CountingRevoker:
        return CountingRevoker(revoker_calls, 2)

    return SqlDocumentsUnitOfWorkFactory(make_wiring(app_engine, clock, ids).factory, revokers)
