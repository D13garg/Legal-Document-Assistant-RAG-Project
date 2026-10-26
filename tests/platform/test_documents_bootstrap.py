"""Documents wired through the real composition root on real Postgres (ADR-005/015/023).

Identity, Access Control, Audit and Documents all run on their real SQL adapters. These are the
cross-module proofs: tombstone and revocation are ONE transaction (including when audit or the
revoker fails), purge removes only Documents' data and leaves audit untouched, and an upload that
dies half-way is recovered by the delayed intent job.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    AccessControlContainer,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.documents import (
    DOCUMENTS_MIGRATIONS,
    DocsContainer,
    build_docs,
    load_docs_settings,
    register_doc_resources,
)
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.bootstrap.ingestion import INGESTION_MIGRATIONS, build_ingestion, load_ingestion_settings
from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    GrantAccessCommand,
    ResourceRef,
    ResourceRegistry,
)
from lda.modules.access_control.infrastructure.persistence.unit_of_work import (
    SqlResourceAccessRevoker,
)
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.documents.api import (
    DELETE_DOCUMENT,
    PURGE_JOB,
    READ_DOCUMENT,
    UPLOAD_INTENT_JOB,
    AddDocumentVersionCommand,
    DocumentReference,
    UploadDocumentCommand,
)
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.testing import ScriptedScanner, ScriptedStage
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.errors import InfrastructureError, NotFoundError
from lda.shared.kernel.ids import JobId, RequestId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.jobs import ClaimedJob, Succeeded
from lda.shared.ports.object_store import TenantScopedKey
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"documents-bootstrap-test-key-32!").decode().rstrip("=")
T1 = TenantId(UUID(int=0x71))
PDF = "application/pdf"
BODY = b"%PDF-1.7 TOP-SECRET clause"
TITLE = "Privileged merger memo"
START = datetime(2026, 1, 1, tzinfo=UTC)


def environment(db: Database) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
    }


class Store:
    """In-memory ObjectStore with a switch that makes `delete` fail (a dying process)."""

    def __init__(self) -> None:
        from lda.platform.testing.doubles import InMemoryObjectStore

        self.inner = InMemoryObjectStore()
        self.fail_delete = False
        self.fail_put_after_write = False

    def put(self, key: TenantScopedKey, body: bytes, *, content_type: str, overwrite: bool = False):  # type: ignore[no-untyped-def]
        stored = self.inner.put(key, body, content_type=content_type, overwrite=overwrite)
        if self.fail_put_after_write:
            raise InfrastructureError("process_died_after_put")
        return stored

    def get(self, key: TenantScopedKey) -> bytes:
        return self.inner.get(key)

    def exists(self, key: TenantScopedKey) -> bool:
        return self.inner.exists(key)

    def delete(self, key: TenantScopedKey) -> None:
        if self.fail_delete:
            raise InfrastructureError("object_store_unavailable")
        self.inner.delete(key)

    def presign_get(self, key: TenantScopedKey, *, expires_seconds: int) -> str:
        return self.inner.presign_get(key, expires_seconds=expires_seconds)


class World:
    def __init__(self, db: Database, *, audit_factory=None, revoker_factory=None) -> None:  # type: ignore[no-untyped-def]
        config = ConfigurationLoader(environment(db))
        settings = load_access_control_settings(config)
        self.platform = build_platform(config, audit_factory=audit_factory or SqlAuditRecorder)
        self.store = Store()
        self.clock = FakeClock(START)
        platform: PlatformContainer = dataclasses.replace(
            self.platform,
            object_store=self.store,
            clock=self.clock,
        )
        identity = build_identity(self.platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        self.ac: AccessControlContainer = build_access_control(
            self.platform, identity, settings, registry
        )
        if revoker_factory is not None:
            self.ac = dataclasses.replace(self.ac, revoker_factory=revoker_factory)
        ingestion = build_ingestion(
            platform,
            load_ingestion_settings(config),
            scanner=ScriptedScanner(),
            stages=[ScriptedStage("fake_stage")],
        )
        self.docs: DocsContainer = build_docs(
            platform, self.ac, ingestion, load_docs_settings(config)
        )
        self.identity = identity
        admin = SystemContext(T1, "documents-e2e")
        self.alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
        ).principal_id
        self.bob = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Bob")
        ).principal_id
        self.ctx = TenantContext(T1, self.alice, RequestId(UUID(int=9)))

    def close(self) -> None:
        self.platform.dispose()

    def ref(self, document_id: object) -> ResourceRef:
        return ResourceRef("document", str(document_id))

    def grant(self, document_id: object, principal: object, action: str) -> None:
        self.ac.grant_access.execute(
            self.ctx,
            GrantAccessCommand(principal, self.ref(document_id), ActionId(action)),  # type: ignore[arg-type]
        )

    def allowed(self, document_id: object, action: str = READ_DOCUMENT) -> bool:
        request = AuthorizationRequest(ActionId(action), frozenset({self.ref(document_id)}))
        return self.ac.authorize_action.execute(self.ctx, request).allowed


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_documents_boot",
        "lda_app_documents_boot",
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


@pytest.fixture
def world(database: Database, oracle: Engine) -> Iterator[World]:
    w = World(database)
    yield w
    w.close()


def count(oracle: Engine, sql: str, **params: object) -> int:
    with oracle.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def epoch(oracle: Engine) -> int:
    return count(oracle, "SELECT coalesce(max(epoch), 0) FROM access_control.policy_epochs")


def grants(oracle: Engine, document_id: object) -> int:
    return count(
        oracle,
        "SELECT count(*) FROM access_control.grants WHERE resource_id = :r",
        r=str(document_id),
    )


def claimed(job_type: str, payload: dict[str, str]) -> ClaimedJob:
    return ClaimedJob(
        id=JobId(UUID(int=1)),
        tenant_id=T1,
        queue="documents",
        job_type=job_type,
        payload=payload,
        attempt=1,
        max_attempts=5,
        fencing_token=1,
    )


def upload(w: World) -> tuple[object, object]:
    view = w.docs.upload_document.execute(w.ctx, UploadDocumentCommand(TITLE, PDF, BODY))
    return view.document_id, view.version_id


def key(document_id: object, version_id: object) -> TenantScopedKey:
    return TenantScopedKey(T1, f"documents/{document_id}/{version_id}/original")


def test_upload_delete_and_purge_end_to_end(world: World, oracle: Engine) -> None:
    w = world
    document_id, version_id = upload(w)
    assert w.store.exists(key(document_id, version_id))
    assert count(oracle, "SELECT count(*) FROM documents.upload_intents") == 0
    for principal, action in (
        (w.alice, READ_DOCUMENT),
        (w.alice, DELETE_DOCUMENT),
        (w.bob, READ_DOCUMENT),
    ):
        w.grant(document_id, principal, action)
    assert w.allowed(document_id) and grants(oracle, document_id) == 3
    epoch_before = epoch(oracle)

    deleted = w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]

    assert (deleted.changed, deleted.revoked_grants) == (True, 3)
    assert grants(oracle, document_id) == 0 and epoch(oracle) == epoch_before + 1  # advanced ONCE
    assert w.allowed(document_id) is False  # access is gone the moment the tombstone commits
    with pytest.raises(NotFoundError):
        w.docs.get_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
    assert w.store.exists(key(document_id, version_id))  # bytes live until the purge job runs
    purge_jobs = count(
        oracle, "SELECT count(*) FROM platform.jobs WHERE job_type = :t", t=PURGE_JOB
    )
    assert purge_jobs == 1
    again = w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
    assert again.changed is False and epoch(oracle) == epoch_before + 1
    assert count(oracle, "SELECT count(*) FROM platform.jobs WHERE job_type = :t", t=PURGE_JOB) == 1

    audit_before = count(oracle, "SELECT count(*) FROM audit.records")
    outcome = w.docs.job_handlers[PURGE_JOB].handle(
        claimed(PURGE_JOB, {"document_id": str(document_id)}), SystemContext(T1, "documents.purge")
    )

    assert outcome == Succeeded()
    assert not w.store.exists(key(document_id, version_id))
    assert count(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert count(oracle, "SELECT count(*) FROM documents.versions") == 0
    receipt = count(
        oracle,
        "SELECT count(*) FROM documents.purge_receipts WHERE scope = :s",
        s="documents.originals_and_rows",
    )
    assert receipt == 1
    verification = w.docs.verify_document_purge.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
    assert verification.verified
    # audit: exactly one fact per effective change, in order; the purge added only its own fact
    with oracle.connect() as conn:
        actions = [
            r[0] for r in conn.execute(text("SELECT action FROM audit.records ORDER BY record_id"))
        ]
        dump = repr(conn.execute(text("SELECT attributes::text FROM audit.records")).all())
    assert actions == [
        "identity.principal_registered",
        "identity.principal_registered",
        "documents.document_uploaded",
        "access_control.access_granted",
        "access_control.access_granted",
        "access_control.access_granted",
        "documents.document_deleted",
        "documents.document_purged",
    ]
    assert count(oracle, "SELECT count(*) FROM audit.records") == audit_before + 1
    assert TITLE not in dump and "merger" not in dump and "SECRET" not in dump


def test_a_failing_revoker_leaves_the_document_active_with_all_its_grants(
    database: Database, oracle: Engine
) -> None:
    class Broken:
        def __init__(self, tx: TenantTransaction) -> None:
            self._real = SqlResourceAccessRevoker(tx)

        def revoke_all(self, resource_kind: str, resource_id: str) -> int:
            self._real.revoke_all(resource_kind, resource_id)  # really removes, then fails
            raise InfrastructureError("access_control_persistence_failed")

    w = World(database, revoker_factory=Broken)
    try:
        document_id, _ = upload(w)
        w.grant(document_id, w.alice, READ_DOCUMENT)
        w.grant(document_id, w.bob, READ_DOCUMENT)
        epoch_before = epoch(oracle)
        with pytest.raises(InfrastructureError):
            w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
        assert (
            count(oracle, "SELECT count(*) FROM documents.documents WHERE status = 'active'") == 1
        )
        assert grants(oracle, document_id) == 2 and epoch(oracle) == epoch_before
        assert w.allowed(document_id)
        assert (
            count(oracle, "SELECT count(*) FROM platform.jobs WHERE job_type = :t", t=PURGE_JOB)
            == 0
        )
        assert (
            count(
                oracle,
                "SELECT count(*) FROM audit.records WHERE action = 'documents.document_deleted'",
            )
            == 0
        )
    finally:
        w.close()


def test_a_failing_audit_write_rolls_back_the_tombstone_and_the_revocation(
    database: Database, oracle: Engine
) -> None:
    class AuditDiesOnDelete:
        def __init__(self, tx: TenantTransaction) -> None:
            self._real = SqlAuditRecorder(tx)

        def record(self, fact: AuditFact) -> None:
            if fact.action == "documents.document_deleted":
                raise InfrastructureError("audit_persistence_failed")
            self._real.record(fact)

    w = World(database, audit_factory=AuditDiesOnDelete)
    try:
        document_id, _ = upload(w)
        w.grant(document_id, w.alice, READ_DOCUMENT)
        w.grant(document_id, w.bob, READ_DOCUMENT)
        epoch_before = epoch(oracle)
        with pytest.raises(InfrastructureError) as caught:
            w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
        assert caught.value.code == "audit_persistence_failed"
        assert (
            count(oracle, "SELECT count(*) FROM documents.documents WHERE status = 'active'") == 1
        )
        assert grants(oracle, document_id) == 2 and epoch(oracle) == epoch_before
        assert w.allowed(document_id)  # access was never lost
    finally:
        w.close()


def test_versions_accumulate_and_the_purge_removes_all_of_them(
    world: World, oracle: Engine
) -> None:
    w = world
    document_id, first = upload(w)
    second = w.docs.add_document_version.execute(
        w.ctx,
        AddDocumentVersionCommand(document_id, "text/plain", b"v2"),  # type: ignore[arg-type]
    ).version_id
    assert count(oracle, "SELECT count(*) FROM documents.versions") == 2
    w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
    w.docs.job_handlers[PURGE_JOB].handle(
        claimed(PURGE_JOB, {"document_id": str(document_id)}), SystemContext(T1, "documents.purge")
    )
    assert not w.store.exists(key(document_id, first)) and not w.store.exists(
        key(document_id, second)
    )
    assert count(oracle, "SELECT count(*) FROM documents.versions") == 0
    with oracle.connect() as conn:
        ids = conn.execute(
            text("SELECT version_ids::text FROM documents.purge_receipts")
        ).scalar_one()
    assert str(first) in ids and str(second) in ids


def test_a_purge_interrupted_by_a_failing_store_is_retried_to_completion(
    world: World, oracle: Engine
) -> None:
    w = world
    document_id, version_id = upload(w)
    w.docs.delete_document.execute(w.ctx, DocumentReference(document_id))  # type: ignore[arg-type]
    handler = w.docs.job_handlers[PURGE_JOB]
    job = claimed(PURGE_JOB, {"document_id": str(document_id)})
    w.store.fail_delete = True
    with pytest.raises(InfrastructureError):
        handler.handle(job, SystemContext(T1, "documents.purge"))
    assert count(oracle, "SELECT count(*) FROM documents.documents") == 1
    assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 0
    w.store.fail_delete = False
    assert handler.handle(job, SystemContext(T1, "documents.purge")) == Succeeded()
    assert count(oracle, "SELECT count(*) FROM documents.purge_receipts") == 1
    assert not w.store.exists(key(document_id, version_id))


def test_an_upload_that_dies_after_the_object_landed_is_recovered_by_its_intent_job(
    world: World, oracle: Engine
) -> None:
    w = world
    w.store.fail_put_after_write = True  # bytes land, then the "process dies"
    w.store.fail_delete = True  # ... and cannot even clean up after itself
    with pytest.raises(InfrastructureError):
        w.docs.upload_document.execute(w.ctx, UploadDocumentCommand(TITLE, PDF, BODY))
    assert count(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert (
        count(oracle, "SELECT count(*) FROM documents.upload_intents") == 1
    )  # findable from the DB
    with oracle.connect() as conn:
        job = conn.execute(
            text("SELECT job_type, payload, run_at FROM platform.jobs WHERE job_type = :t"),
            {"t": UPLOAD_INTENT_JOB},
        ).one()
    assert job[2] == START + timedelta(seconds=900)  # delayed by the grace period
    payload = job[1]
    handler = w.docs.job_handlers[UPLOAD_INTENT_JOB]
    claimed_job = claimed(UPLOAD_INTENT_JOB, dict(payload))
    key_ = key(payload["document_id"], payload["version_id"])
    assert w.store.exists(key_)  # the orphan exists ...
    w.store.fail_delete = False
    w.store.fail_put_after_write = False
    early = handler.handle(claimed_job, SystemContext(T1, "documents.reaper"))
    assert early != Succeeded() and w.store.exists(key_)  # not before the grace
    w.clock.advance(timedelta(seconds=900))
    assert handler.handle(claimed_job, SystemContext(T1, "documents.reaper")) == Succeeded()
    assert not w.store.exists(key_)  # ... and is gone, with no manual step
    assert count(oracle, "SELECT count(*) FROM documents.upload_intents") == 0


def test_tenants_are_isolated_through_the_whole_stack(world: World, oracle: Engine) -> None:
    w = world
    document_id, _ = upload(w)
    other = TenantContext(TenantId(UUID(int=0x72)), w.alice, RequestId(UUID(int=2)))
    with pytest.raises(NotFoundError):
        w.docs.get_document.execute(other, DocumentReference(document_id))  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        w.docs.delete_document.execute(other, DocumentReference(document_id))  # type: ignore[arg-type]
    assert count(oracle, "SELECT count(*) FROM documents.documents WHERE status = 'active'") == 1
