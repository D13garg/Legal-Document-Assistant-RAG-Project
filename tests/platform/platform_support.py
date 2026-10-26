from __future__ import annotations

import importlib
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from lda.platform.database import migrate
from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.transaction import TenantTransaction
from lda.platform.database.unit_of_work import AuditRecorderFactory, SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.testing.doubles import (
    FakeClock,
    InMemoryAuditRecorder,
    SequentialIdGenerator,
)
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.jobs import JobSpec

APP_ROLE = "lda_app"
APP_PASSWORD = "lda_app_pw"  # noqa: S105 - throwaway test credential


@dataclass(frozen=True)
class Database:
    admin_url: str
    app_url: str


def _server_url() -> Iterator[str]:
    url = os.environ.get("LDA_TEST_POSTGRES_URL")
    if url:
        yield url
        return
    pgserver = importlib.import_module("pgserver")  # dev dependency; Any-typed on purpose
    with tempfile.TemporaryDirectory() as data_dir:
        server = pgserver.get_server(data_dir, cleanup_mode="delete")
        yield server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        server.cleanup()


@contextmanager
def provisioned_database(
    name: str = "lda_test",
    role: str = APP_ROLE,
    extra_locations: Sequence[str] = (),
) -> Iterator[Database]:
    """A migrated database plus a non-privileged runtime role.

    Distinct `name`/`role` let several suites share one Postgres server (CI service container).
    """
    for server_url in _server_url():
        base = make_url(server_url)
        root = create_engine(base, isolation_level="AUTOCOMMIT")
        with root.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {name}"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
            conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{APP_PASSWORD}'"))
        admin = base.set(database=name)
        app = admin.set(username=role, password=APP_PASSWORD)
        admin_url = admin.render_as_string(hide_password=False)
        migrate.upgrade(admin_url, app_role=role, extra_locations=extra_locations)
        try:
            yield Database(admin_url, app.render_as_string(hide_password=False))
        finally:
            root.dispose()


def tenant(n: int) -> TenantId:
    return TenantId(UUID(int=0xAAAA0000 + n))


@dataclass(frozen=True)
class Wiring:
    factory: SqlUnitOfWorkFactory
    registry: HandlerRegistry
    codec: EventCodec


def make_wiring(
    engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    audit_factory: AuditRecorderFactory | None = None,
) -> Wiring:
    registry, codec = HandlerRegistry(), EventCodec()
    factory = SqlUnitOfWorkFactory(
        engine,
        clock,
        ids,
        audit_factory or (lambda tx: InMemoryAuditRecorder()),
        lambda uow, jobs: TransactionalEventPublisher(registry, codec, jobs, uow),
    )
    return Wiring(factory, registry, codec)


def enqueue_job(
    engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, n: int, spec: JobSpec
) -> JobId | None:
    with TenantTransaction(engine, tenant(n)) as tx:
        job_id = PostgresJobQueue(tx, clock, ids).enqueue(spec)
        tx.commit()
    return job_id


@dataclass(frozen=True)
class JobRow:
    status: str
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    run_at: datetime
    finished_at: datetime | None
    payload: dict[str, object]
    tenant_id: UUID


def job_rows(admin: Engine) -> list[JobRow]:
    """All jobs, read as the table owner (bypasses RLS on purpose: this is the test's oracle)."""
    with admin.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT status, attempts, lease_owner, lease_expires_at, last_error_code, run_at, "
                "finished_at, payload, tenant_id FROM platform.jobs ORDER BY created_at, id"
            )
        ).all()
    return [
        JobRow(
            r.status,
            r.attempts,
            r.lease_owner,
            r.lease_expires_at,
            r.last_error_code,
            r.run_at,
            r.finished_at,
            r.payload,
            r.tenant_id,
        )
        for r in rows
    ]
