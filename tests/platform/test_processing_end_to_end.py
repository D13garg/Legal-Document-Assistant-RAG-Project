"""Step 7 end to end: upload -> ingestion -> the REAL `extract` stage on the REAL Postgres queue,
worker and unit of work. The scanner is a scripted test double (no real scanner exists yet), so
this proves the pipeline mechanics, not production readiness.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.documents import (
    DOCUMENTS_MIGRATIONS,
    DocsContainer,
    build_docs,
    build_original_reader,
    load_docs_settings,
    register_doc_resources,
)
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.bootstrap.ingestion import (
    INGESTION_MIGRATIONS,
    IngestionContainer,
    build_ingestion,
    load_ingestion_settings,
)
from lda.bootstrap.processing import (
    PROCESSING_MIGRATIONS,
    ProcessingContainer,
    build_processing,
    load_processing_settings,
)
from lda.modules.access_control.api import ResourceRegistry
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.document_processing.api import ExtractedDocument
from lda.modules.documents.api import DocumentId, DocumentReference, UploadDocumentCommand
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.api import ScanVerdict
from lda.modules.ingestion.testing import ScriptedScanner
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.unit_of_work import SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.ports.object_store import TenantScopedKey
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"processing-e2e-test-key-32-bytes").decode().rstrip("=")
T1, T2 = TenantId(UUID(int=0x91)), TenantId(UUID(int=0x92))
START = datetime(2026, 1, 1, tzinfo=UTC)
TEXT = "text/plain"
PDF = "application/pdf"
CLAUSE = "TOP-SECRET indemnity clause"
BODY = f"{CLAUSE}\n\nSecond paragraph.".encode()


def _pdf(lines: list[str] | None) -> bytes:
    from lda.modules.document_processing.tests.builders import make_pdf

    return make_pdf([lines])


class Stack:
    def __init__(self, db: Database, scanner: ScriptedScanner | None = None) -> None:
        env = {
            "LDA_DATABASE__URL": db.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
            "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
            "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        }
        config = ConfigurationLoader(env)
        base = build_platform(config, audit_factory=SqlAuditRecorder)
        self.engine = create_engine(db.app_url)
        self.clock = FakeClock(START)
        handlers, codec = HandlerRegistry(), EventCodec()
        units = SqlUnitOfWorkFactory(
            self.engine,
            self.clock,
            base.ids,
            SqlAuditRecorder,
            lambda uow, jobs: TransactionalEventPublisher(handlers, codec, jobs, uow),
        )
        self.objects = InMemoryObjectStore()
        self.platform: PlatformContainer = dataclasses.replace(
            base,
            clock=self.clock,
            units_of_work=units,
            handlers=handlers,
            event_codec=codec,
            job_leases=JobLeaseStore(self.engine, self.clock),
            job_maintenance=JobMaintenance(self.engine, self.clock),
            object_store=self.objects,
        )
        self._base = base
        identity = build_identity(self.platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        access = build_access_control(
            self.platform, identity, load_access_control_settings(config), registry
        )
        # Order matters: reader -> processing (the stage) -> ingestion (the plan) -> docs.
        reader = build_original_reader(self.platform, access)
        self.processing: ProcessingContainer = build_processing(
            self.platform, reader, load_processing_settings(config)
        )
        self.scanner = scanner or ScriptedScanner()
        self.ingestion: IngestionContainer = build_ingestion(
            self.platform,
            load_ingestion_settings(config),
            scanner=self.scanner,
            stages=[self.processing.stage],
        )
        self.docs: DocsContainer = build_docs(
            self.platform, access, self.ingestion, load_docs_settings(config)
        )
        self.identity = identity
        admin = SystemContext(T1, "processing-e2e")
        self.alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
        ).principal_id
        self.handlers = {
            **self.docs.job_handlers,
            **self.ingestion.job_handlers,
            **self.processing.job_handlers,
        }
        self.worker = JobWorker(
            self.platform.job_leases,
            self.handlers,
            self.platform.job_settings,
            self.clock,
            "worker-1",
            BackoffPolicy(1.0, 10.0, jitter=lambda: 0.0),
            self.platform.job_maintenance,
        )

    def ctx(self, tenant: TenantId = T1) -> TenantContext:
        return TenantContext(tenant, self.alice, RequestId(UUID(int=9)))

    def upload(
        self, body: bytes = BODY, media_type: str = TEXT, tenant: TenantId = T1
    ) -> tuple[UUID, UUID]:
        view = self.docs.upload_document.execute(
            self.ctx(tenant), UploadDocumentCommand("Merger memo", media_type, body)
        )
        return view.document_id.value, view.version_id.value

    def drain(self, rounds: int = 60) -> None:
        for _ in range(rounds):
            if self.worker.run_once() == 0:
                break

    def close(self) -> None:
        self._base.dispose()
        self.engine.dispose()


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_processing_e2e",
        "lda_app_processing_e2e",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
            PROCESSING_MIGRATIONS,
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
            "document_processing.purge_receipts",
            "document_processing.extractions",
            "documents.purge_receipts",
            "documents.upload_intents",
            "documents.versions",
            "documents.documents",
            "access_control.grants",
            "access_control.policy_epochs",
            "identity.principals",
            "ingestion.stage_runs",
            "ingestion.runs",
            "platform.jobs",
        ):
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
    yield engine
    engine.dispose()


@pytest.fixture
def stack(database: Database, oracle: Engine) -> Iterator[Stack]:
    s = Stack(database)
    yield s
    s.close()


def scalar(oracle: Engine, sql: str, **params: object) -> object:
    with oracle.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def fetch(oracle: Engine, sql: str) -> list[tuple[object, ...]]:
    with oracle.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql)).all()]


def version_status(oracle: Engine, version_id: UUID) -> str:
    return str(
        scalar(
            oracle, "SELECT status FROM documents.versions WHERE version_id = :v", v=str(version_id)
        )
    )


def artifact_key(tenant: TenantId, document: UUID, version: UUID) -> TenantScopedKey:
    return TenantScopedKey(tenant, f"document_processing/{document}/{version}/extracted")


def test_a_text_document_is_extracted_and_only_then_becomes_ready(
    stack: Stack, oracle: Engine
) -> None:
    document, version = stack.upload()
    assert version_status(oracle, version) == "pending"
    stack.drain()
    assert version_status(oracle, version) == "ready"
    artifact = ExtractedDocument.from_bytes(stack.objects.get(artifact_key(T1, document, version)))
    assert [b.text for b in artifact.blocks] == [CLAUSE, "Second paragraph."]
    (row,) = fetch(
        oracle,
        "SELECT block_count, character_count, media_type FROM document_processing.extractions",
    )
    assert row == (2, len(CLAUSE) + len("Second paragraph."), TEXT)
    assert scalar(oracle, "SELECT status FROM ingestion.runs") == "completed"
    stages = {
        r[0] for r in oracle.connect().execute(text("SELECT status FROM ingestion.stage_runs"))
    }
    assert stages == {"succeeded"}


def test_a_pdf_document_is_extracted_with_pages(stack: Stack, oracle: Engine) -> None:
    document, version = stack.upload(_pdf(["Clause one", "continues"]), PDF)
    stack.drain()
    assert version_status(oracle, version) == "ready"
    artifact = ExtractedDocument.from_bytes(stack.objects.get(artifact_key(T1, document, version)))
    assert artifact.page_count == 1 and artifact.blocks[0].page == 1


def test_no_content_reaches_jobs_events_audit_or_the_database(stack: Stack, oracle: Engine) -> None:
    stack.upload()
    stack.drain()
    with oracle.connect() as conn:
        dumps = [
            str(
                conn.execute(
                    text("SELECT coalesce(string_agg(payload::text, ''), '') FROM platform.jobs")
                ).scalar_one()
            ),
            str(
                conn.execute(
                    text("SELECT coalesce(string_agg(r::text, ''), '') FROM audit.records r")
                ).scalar_one()
            ),
            str(
                conn.execute(
                    text(
                        "SELECT coalesce(string_agg(e::text, ''), '') FROM document_processing.extractions e"
                    )
                ).scalar_one()
            ),
            str(
                conn.execute(
                    text("SELECT coalesce(string_agg(e::text, ''), '') FROM ingestion.runs e")
                ).scalar_one()
            ),
            str(
                conn.execute(
                    text("SELECT coalesce(string_agg(e::text, ''), '') FROM ingestion.stage_runs e")
                ).scalar_one()
            ),
        ]
    assert all("TOP-SECRET" not in d and "Second paragraph" not in d for d in dumps)


@pytest.mark.parametrize(
    ("body", "media_type", "code"),
    [
        (b"%PDF-1.4 this is not a pdf", PDF, "malformed_document"),
        (_pdf(None), PDF, "no_extractable_text"),
        (b" \n\t ", TEXT, "empty_document"),
        (b"caf\xe9", TEXT, "malformed_document"),
    ],
)
def test_a_permanent_extraction_failure_fails_the_version_and_never_readies_it(
    stack: Stack, oracle: Engine, body: bytes, media_type: str, code: str
) -> None:
    document, version = stack.upload(body, media_type)
    stack.drain()
    assert version_status(oracle, version) == "failed"
    assert scalar(oracle, "SELECT status FROM ingestion.runs") == "failed"
    assert (
        scalar(oracle, "SELECT error_code FROM ingestion.stage_runs WHERE stage = 'extract'")
        == code
    )
    assert scalar(oracle, "SELECT count(*) FROM document_processing.extractions") == 0
    assert not stack.objects.exists(artifact_key(T1, document, version))
    assert scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0


def test_an_infected_document_is_quarantined_and_never_extracted(
    database: Database, oracle: Engine
) -> None:
    s = Stack(database, ScriptedScanner([ScanVerdict.INFECTED]))
    try:
        document, version = s.upload()
        s.drain()
        assert version_status(oracle, version) == "quarantined"
        assert scalar(oracle, "SELECT count(*) FROM document_processing.extractions") == 0
        assert not s.objects.exists(artifact_key(T1, document, version))
    finally:
        s.close()


def test_delete_and_purge_remove_the_extraction_and_leave_a_receipt(
    stack: Stack, oracle: Engine
) -> None:
    document, version = stack.upload()
    stack.drain()
    assert stack.objects.exists(artifact_key(T1, document, version))
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document)))
    stack.drain()
    assert scalar(oracle, "SELECT count(*) FROM documents.documents") == 0  # purged by Documents
    assert scalar(oracle, "SELECT count(*) FROM document_processing.extractions") == 0
    assert not stack.objects.exists(artifact_key(T1, document, version))
    assert scalar(oracle, "SELECT count(*) FROM document_processing.purge_receipts") == 1
    assert scalar(oracle, "SELECT versions_purged FROM document_processing.purge_receipts") == 1
    assert scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0


def test_tenants_are_isolated_end_to_end(stack: Stack, oracle: Engine) -> None:
    other = stack.identity.register_principal.execute(
        SystemContext(T2, "processing-e2e"), RegisterPrincipalCommand(PrincipalKind.USER, "Bob")
    ).principal_id
    doc1, ver1 = stack.upload(tenant=T1)
    view = stack.docs.upload_document.execute(
        TenantContext(T2, other, RequestId(UUID(int=10))),
        UploadDocumentCommand("Other", TEXT, BODY),
    )
    doc2, ver2 = view.document_id.value, view.version_id.value
    stack.drain()
    assert version_status(oracle, ver1) == version_status(oracle, ver2) == "ready"
    assert scalar(oracle, "SELECT count(*) FROM document_processing.extractions") == 2
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(doc1)))
    stack.drain()
    # tenant 1's derived data is gone; tenant 2's identical content is untouched
    assert not stack.objects.exists(artifact_key(T1, doc1, ver1))
    assert stack.objects.exists(artifact_key(T2, doc2, ver2))
    rows = [
        (str(r[0]), str(r[1]))
        for r in fetch(oracle, "SELECT tenant_id, version_id FROM document_processing.extractions")
    ]
    assert rows == [(str(T2.value), str(ver2))]
    assert version_status(oracle, ver2) == "ready"
