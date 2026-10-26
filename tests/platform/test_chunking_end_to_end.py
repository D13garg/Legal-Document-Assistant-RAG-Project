"""Step 8 end to end: upload -> ingestion -> REAL `extract` then REAL `chunk` stages on the REAL
Postgres queue, worker and unit of work. The scanner is a scripted test double (no real scanner
exists yet), so this proves the pipeline mechanics, not production readiness.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from collections.abc import Iterator, Sequence
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
from lda.bootstrap.chunking import (
    CHUNKING_MIGRATIONS,
    ChunkingContainer,
    build_chunking,
    load_chunking_settings,
)
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.documents import (
    DOCUMENTS_MIGRATIONS,
    DocsContainer,
    build_docs,
    build_original_reader,
    build_version_state_reader,
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
    build_extraction_reader,
    build_processing,
    load_processing_settings,
)
from lda.modules.access_control.api import ResourceRegistry
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.chunking.config import ChunkingSettings
from lda.modules.document_processing.api import ExtractedDocument
from lda.modules.documents.api import DocumentId, DocumentReference, UploadDocumentCommand
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.api import PipelineStage, ScanVerdict
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
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import RequestId, TenantId
from lda.shared.ports.object_store import TenantScopedKey
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"chunking-e2e-test-key-32-bytes!!").decode().rstrip("=")
T1, T2 = TenantId(UUID(int=0xA1)), TenantId(UUID(int=0xA2))
START = datetime(2026, 1, 1, tzinfo=UTC)
TEXT = "text/plain"
CLAUSE = "TOP-SECRET indemnity clause"
BODY = f"{CLAUSE}\n\nSecond paragraph.".encode()
LONG_BODY = "\n\n".join(f"Paragraph {n}. " + "Sentence of the agreement. " * 20 for n in range(12))


class Stack:
    def __init__(
        self,
        db: Database,
        scanner: ScriptedScanner | None = None,
        order: Sequence[str] = ("extract", "chunk"),
    ) -> None:
        env = {
            "LDA_DATABASE__URL": db.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
            "LDA_CHUNKING__MAX_CHARACTERS": "300",
            "LDA_CHUNKING__MIN_CHARACTERS": "60",
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
        versions = build_version_state_reader(self.platform, access)
        self.chunking: ChunkingContainer = build_chunking(
            self.platform,
            build_extraction_reader(self.platform, versions),
            load_chunking_settings(config),
        )
        stages: dict[str, PipelineStage] = {
            "extract": self.processing.stage,
            "chunk": self.chunking.stage,
        }
        self.scanner = scanner or ScriptedScanner()
        self.ingestion: IngestionContainer = build_ingestion(
            self.platform,
            load_ingestion_settings(config),
            scanner=self.scanner,
            stages=[stages[name] for name in order],
        )
        self.docs: DocsContainer = build_docs(
            self.platform, access, self.ingestion, load_docs_settings(config)
        )
        self.identity = identity
        self.access = access
        admin = SystemContext(T1, "chunking-e2e")
        self.alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
        ).principal_id
        self.handlers = {
            **self.docs.job_handlers,
            **self.ingestion.job_handlers,
            **self.processing.job_handlers,
            **self.chunking.job_handlers,
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
        "lda_test_chunking_e2e",
        "lda_app_chunking_e2e",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
            PROCESSING_MIGRATIONS,
            CHUNKING_MIGRATIONS,
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
            "chunking.purge_receipts",
            "chunking.chunk_sets",
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


def version_status(oracle: Engine, version_id: UUID) -> str:
    return str(
        scalar(
            oracle, "SELECT status FROM documents.versions WHERE version_id = :v", v=str(version_id)
        )
    )


def chunk_key(tenant: TenantId, document: UUID, version: UUID) -> TenantScopedKey:
    return TenantScopedKey(tenant, f"chunking/{document}/{version}/chunks")


def extraction_key(tenant: TenantId, document: UUID, version: UUID) -> TenantScopedKey:
    return TenantScopedKey(tenant, f"document_processing/{document}/{version}/extracted")


def test_a_document_is_extracted_chunked_and_only_then_ready(stack: Stack, oracle: Engine) -> None:
    document, version = stack.upload(LONG_BODY.encode())
    assert version_status(oracle, version) == "pending"
    stack.drain()
    assert version_status(oracle, version) == "ready"
    stages = oracle.connect().execute(
        text("SELECT stage, status FROM ingestion.stage_runs ORDER BY stage")
    )
    assert {tuple(r) for r in stages} == {
        ("chunk", "succeeded"),
        ("extract", "succeeded"),
        ("scan", "succeeded"),
    }
    payload = json.loads(stack.objects.get(chunk_key(T1, document, version)))
    blocks = ExtractedDocument.from_bytes(
        stack.objects.get(extraction_key(T1, document, version))
    ).blocks
    assert len(payload["chunks"]) > 3
    for chunk in payload["chunks"]:  # every boundary reproduces its text from the extraction
        assert chunk["text"] == "\n\n".join(blocks[b].text[s:e] for b, s, e in chunk["segments"])
        assert 0 < len(chunk["text"]) <= 300
    (row,) = [
        tuple(r)
        for r in oracle.connect().execute(
            text(
                "SELECT c.chunk_count, c.extraction_hash = e.content_hash "
                "FROM chunking.chunk_sets c JOIN document_processing.extractions e "
                "USING (tenant_id, version_id)"
            )
        )
    ]
    assert row == (len(payload["chunks"]), True)  # the set names the extraction it came from


def test_a_failed_extraction_never_reaches_the_chunk_stage(stack: Stack, oracle: Engine) -> None:
    document, version = stack.upload(b" \n\t ")
    stack.drain()
    assert version_status(oracle, version) == "failed"
    assert (  # the frozen plan lists the stage, but it never started
        scalar(oracle, "SELECT status FROM ingestion.stage_runs WHERE stage = 'chunk'") == "pending"
    )
    assert scalar(oracle, "SELECT count(*) FROM chunking.chunk_sets") == 0
    assert not stack.objects.exists(chunk_key(T1, document, version))


def test_a_plan_with_chunk_before_extract_fails_closed(database: Database, oracle: Engine) -> None:
    s = Stack(database, order=("chunk", "extract"))
    try:
        document, version = s.upload()
        s.drain()
        assert version_status(oracle, version) == "failed"  # never ready
        assert (
            scalar(oracle, "SELECT error_code FROM ingestion.stage_runs WHERE stage = 'chunk'")
            == "extraction_unavailable"
        )
        assert not s.objects.exists(chunk_key(T1, document, version))
    finally:
        s.close()


def test_an_infected_document_is_quarantined_and_never_chunked(
    database: Database, oracle: Engine
) -> None:
    s = Stack(database, ScriptedScanner([ScanVerdict.INFECTED]))
    try:
        document, version = s.upload()
        s.drain()
        assert version_status(oracle, version) == "quarantined"
        assert scalar(oracle, "SELECT count(*) FROM chunking.chunk_sets") == 0
        assert not s.objects.exists(chunk_key(T1, document, version))
    finally:
        s.close()


def _dump(table: str) -> str:
    return f"SELECT coalesce(string_agg(r::text, ''), '') FROM {table} r"  # noqa: S608


def test_no_content_reaches_jobs_events_audit_or_the_database(stack: Stack, oracle: Engine) -> None:
    stack.upload()
    stack.drain()
    tables = (
        "platform.jobs",
        "audit.records",
        "chunking.chunk_sets",
        "chunking.purge_receipts",
        "ingestion.runs",
        "ingestion.stage_runs",
    )
    with oracle.connect() as conn:
        dumps = [str(conn.execute(text(_dump(table))).scalar_one()) for table in tables]
    assert all("TOP-SECRET" not in d and "Second paragraph" not in d for d in dumps)


def test_delete_and_purge_remove_the_chunks_and_leave_their_own_receipt(
    stack: Stack, oracle: Engine
) -> None:
    document, version = stack.upload()
    stack.drain()
    assert stack.objects.exists(chunk_key(T1, document, version))
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document)))
    stack.drain()
    assert scalar(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert scalar(oracle, "SELECT count(*) FROM chunking.chunk_sets") == 0
    assert not stack.objects.exists(chunk_key(T1, document, version))
    assert scalar(oracle, "SELECT count(*) FROM chunking.purge_receipts") == 1
    assert scalar(oracle, "SELECT versions_purged FROM chunking.purge_receipts") == 1
    assert scalar(oracle, "SELECT scope FROM chunking.purge_receipts") == (
        "chunking.chunk_artifacts_and_rows"
    )
    assert scalar(oracle, "SELECT count(*) FROM document_processing.purge_receipts") == 1
    assert scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0


def test_tenants_are_isolated_end_to_end(stack: Stack, oracle: Engine) -> None:
    other = stack.identity.register_principal.execute(
        SystemContext(T2, "chunking-e2e"), RegisterPrincipalCommand(PrincipalKind.USER, "Bob")
    ).principal_id
    doc1, ver1 = stack.upload(tenant=T1)
    view = stack.docs.upload_document.execute(
        TenantContext(T2, other, RequestId(UUID(int=10))),
        UploadDocumentCommand("Other", TEXT, BODY),
    )
    doc2, ver2 = view.document_id.value, view.version_id.value
    stack.drain()
    assert version_status(oracle, ver1) == version_status(oracle, ver2) == "ready"
    first = json.loads(stack.objects.get(chunk_key(T1, doc1, ver1)))
    second = json.loads(stack.objects.get(chunk_key(T2, doc2, ver2)))
    assert first["chunks"][0]["text"] == second["chunks"][0]["text"]  # identical content...
    assert first["chunks"][0]["id"] != second["chunks"][0]["id"]  # ...never the same identity
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(doc1)))
    stack.drain()
    assert not stack.objects.exists(chunk_key(T1, doc1, ver1))
    assert stack.objects.exists(chunk_key(T2, doc2, ver2))
    assert scalar(oracle, "SELECT count(*) FROM chunking.chunk_sets") == 1


def test_unsafe_chunking_settings_fail_at_composition(stack: Stack) -> None:
    reader = build_extraction_reader(
        stack.platform, build_version_state_reader(stack.platform, stack.access)
    )
    with pytest.raises(ValidationError):
        build_chunking(stack.platform, reader, ChunkingSettings(max_characters=10))
    with pytest.raises(ValidationError):
        build_chunking(
            stack.platform, reader, ChunkingSettings(max_characters=500, min_characters=400)
        )
