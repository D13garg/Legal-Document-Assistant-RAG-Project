"""Purge crash points and concurrency on real PostgreSQL (ADR-023 section 4; Step 18).

Established before this file: a failing object store is retried to completion. Not covered until
now: (1) the purge's FINAL transaction failing after the originals were already deleted (the
"crash after deleting objects" ADR-023 says a retry repairs), and (2) two workers purging the
same document at once. Both run on the real SQL adapters; the object store is an in-memory
double, so nothing here proves a real object store behaves the same way.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text
from test_documents_bootstrap import (
    T1,
    World,
    claimed,
    count,
    key,
    upload,
)

from lda.bootstrap.access_control import ACCESS_CONTROL_MIGRATIONS
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.documents import DOCUMENTS_MIGRATIONS
from lda.bootstrap.identity import IDENTITY_MIGRATIONS
from lda.bootstrap.ingestion import INGESTION_MIGRATIONS
from lda.modules.access_control.api import ActionId  # noqa: F401
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.documents.api import PURGE_JOB, DocumentReference
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.jobs import Succeeded
from platform_support import Database, provisioned_database


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_purge_recovery",
        "lda_app_purge_recovery",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
        ],
    ) as db:
        yield db


@pytest.fixture
def oracle(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for table in (
            "audit.records",
            "documents.purge_receipts",
            "documents.upload_intents",
            "documents.versions",
            "documents.documents",
            "access_control.grants",
            "access_control.policy_epochs",
            "identity.principals",
            "platform.jobs",
        ):
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
    yield engine
    engine.dispose()


PURGED = "documents.document_purged"


def purge_facts(oracle: Engine) -> int:
    return count(oracle, "SELECT count(*) FROM audit.records WHERE action = :a", a=PURGED)


def test_a_failure_in_the_final_purge_transaction_after_the_originals_are_gone_is_repaired(
    database: Database, oracle: Engine
) -> None:
    class AuditDiesOnPurge:
        failing = True

        def __init__(self, tx: TenantTransaction) -> None:
            self._real = SqlAuditRecorder(tx)

        def record(self, fact: AuditFact) -> None:
            if fact.action == PURGED and AuditDiesOnPurge.failing:
                raise InfrastructureError("audit_persistence_failed")
            self._real.record(fact)

    w = World(database, audit_factory=AuditDiesOnPurge)
    try:
        document_id, version_id = upload(w)
        w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
        handler = w.docs.job_handlers[PURGE_JOB]
        job = claimed(PURGE_JOB, {"document_id": str(document_id)})

        with pytest.raises(InfrastructureError):
            handler.handle(job, SystemContext(T1, "documents.purge"))

        # Crash point: the bytes are already deleted, but the transaction rolled back as a whole.
        assert not w.store.exists(key(document_id, version_id))
        assert count(oracle, "SELECT count(*) FROM documents.documents") == 1
        assert count(oracle, "SELECT count(*) FROM documents.versions") == 1
        assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 0
        assert purge_facts(oracle) == 0  # no receipt, no row removal, no audit fact: all or none
        # Fail closed: the document is still tombstoned and is NOT reported as purged.
        assert (
            count(oracle, "SELECT count(*) FROM documents.documents WHERE status = 'deleted'") == 1
        )

        AuditDiesOnPurge.failing = False  # the "restart"
        assert handler.handle(job, SystemContext(T1, "documents.purge")) == Succeeded()
        assert count(oracle, "SELECT count(*) FROM documents.documents") == 0
        assert count(oracle, "SELECT count(*) FROM documents.versions") == 0
        assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 1
        assert purge_facts(oracle) == 1
        # A further redelivery is a no-op: still one receipt and one fact.
        assert handler.handle(job, SystemContext(T1, "documents.purge")) == Succeeded()
        assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 1
        assert purge_facts(oracle) == 1
    finally:
        w.close()


def test_two_workers_purging_the_same_document_converge_on_one_receipt_and_one_fact(
    database: Database, oracle: Engine
) -> None:
    w = World(database)
    try:
        document_id, version_id = upload(w)
        w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
        handler = w.docs.job_handlers[PURGE_JOB]
        job = claimed(PURGE_JOB, {"document_id": str(document_id)})
        barrier = threading.Barrier(2)
        results: list[object] = []

        def run() -> None:
            barrier.wait()
            try:
                results.append(handler.handle(job, SystemContext(T1, "documents.purge")))
            except Exception as exc:  # a loser may fail; the worker would retry it
                results.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(results) == 2

        # Whatever the interleaving, a retry of any failed attempt must converge and be safe.
        for r in results:
            if not isinstance(r, Succeeded):
                assert isinstance(r, Exception)
                assert handler.handle(job, SystemContext(T1, "documents.purge")) == Succeeded()
        assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 1
        assert purge_facts(oracle) == 1
        assert count(oracle, "SELECT count(*) FROM documents.documents") == 0
        assert not w.store.exists(key(document_id, version_id))
        with oracle.connect() as conn:
            receipts = conn.execute(
                text("SELECT count(DISTINCT document_id) FROM documents.purge_receipts")
            )
            assert receipts.scalar_one() == 1
    finally:
        w.close()
