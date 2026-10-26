"""Step 11 end to end: upload -> ingestion -> REAL `extract`, `chunk` and `index` stages on the REAL
Postgres (pgvector) queue, worker and unit of work, then SEARCH with a REAL signed proof from the
REAL access control. The scanner is a scripted test double and the embedder is the deterministic
fake (no real scanner, provider or embedding egress rule exists), so this proves the mechanics, not
production readiness.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
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
    build_chunk_reader,
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
from lda.bootstrap.retrieval import (
    RETRIEVAL_MIGRATIONS,
    IndexContainer,
    build_index,
    load_index_settings,
)
from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    GrantAccessCommand,
    ResourceRef,
    ResourceRegistry,
    RevokeAccessCommand,
)
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.chunking.api import DocumentId as ChunkDocumentId
from lda.modules.chunking.api import ReadChunksCommand
from lda.modules.chunking.api import VersionId as ChunkVersionId
from lda.modules.documents.api import (
    DOCUMENT_KIND,
    READ_DOCUMENT,
    AddDocumentVersionCommand,
    DocumentId,
    DocumentReference,
    UploadDocumentCommand,
)
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.api import PipelineStage
from lda.modules.ingestion.testing import ScriptedScanner
from lda.modules.retrieval.api import SearchQuery
from lda.modules.retrieval.config import RetrievalSettings
from lda.modules.retrieval.testing import FakeEmbedder
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
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.ids import RequestId, TenantId
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"retrieval-e2e-test-key-32-bytes!").decode().rstrip("=")
ACTION = ActionId(READ_DOCUMENT)
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
        order: Sequence[str] = ("extract", "chunk", "index"),
    ) -> None:
        env = {
            "LDA_DATABASE__URL": db.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
            "LDA_CHUNKING__MAX_CHARACTERS": "300",
            "LDA_CHUNKING__MIN_CHARACTERS": "60",
            "LDA_RETRIEVAL__CANDIDATE_POOL": "5",
            "LDA_RETRIEVAL__MAX_CANDIDATES": "50",
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
        self.embedder = FakeEmbedder()
        self.read_chunks = build_chunk_reader(self.platform, versions)
        self.retrieval: IndexContainer = build_index(
            self.platform,
            chunks=self.read_chunks,
            versions=versions,
            verifier=access.proof_verifier,
            embedder=self.embedder,
            settings=load_index_settings(config),
        )
        stages: dict[str, PipelineStage] = {
            "extract": self.processing.stage,
            "chunk": self.chunking.stage,
            "index": self.retrieval.stage,
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
            **self.retrieval.job_handlers,
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

    def drain(self, rounds: int = 80) -> None:
        for _ in range(rounds):
            if self.worker.run_once() == 0:
                break

    def grant(self, *documents: UUID) -> None:
        for document in documents:
            self.access.grant_access.execute(
                self.ctx(),
                GrantAccessCommand(self.alice, ResourceRef(DOCUMENT_KIND, str(document)), ACTION),
            )

    def decision(self, *documents: UUID):  # type: ignore[no-untyped-def]
        resources = frozenset(ResourceRef(DOCUMENT_KIND, str(d)) for d in documents)
        decision = self.access.resolve_access_scope.execute(
            self.ctx(), AuthorizationRequest(ACTION, resources)
        )
        assert decision.scope is not None and decision.proof is not None
        return decision

    def search(self, text: str, *documents: UUID, limit: int = 5):  # type: ignore[no-untyped-def]
        decision = self.decision(*documents)
        return self.retrieval.search.execute(
            self.ctx(), SearchQuery(decision.scope, decision.proof.token, text, limit)
        )

    def close(self) -> None:
        self._base.dispose()
        self.engine.dispose()


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_retrieval_e2e",
        "lda_app_retrieval_e2e",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
            PROCESSING_MIGRATIONS,
            CHUNKING_MIGRATIONS,
            RETRIEVAL_MIGRATIONS,
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
            "retrieval.purge_receipts",
            "retrieval.chunk_vectors",
            "retrieval.index_versions",
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


def chunk_ids(stack: Stack, document: UUID, version: UUID, tenant: TenantId = T1) -> list[UUID]:
    """The ids Chunking minted, read through the approved `ReadChunks` path."""
    stored = stack.read_chunks.execute(
        SystemContext(tenant, "retrieval-e2e"),
        ReadChunksCommand(ChunkDocumentId(document), ChunkVersionId(version)),
    )
    return [c.chunk_id for c in stored.chunks]


def test_a_document_is_chunked_indexed_and_only_then_ready(stack: Stack, oracle: Engine) -> None:
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
        ("index", "succeeded"),
        ("scan", "succeeded"),
    }
    ids = chunk_ids(stack, document, version)
    assert len(ids) > 3
    assert scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors") == len(ids)
    assert scalar(oracle, "SELECT count(*) FROM retrieval.index_versions") == 1
    indexed = oracle.connect().execute(text("SELECT chunk_id FROM retrieval.chunk_vectors"))
    assert {r[0] for r in indexed} == set(ids)  # exactly the ids Chunking minted


def test_search_returns_authorized_ranked_results_with_provenance(
    stack: Stack, oracle: Engine
) -> None:
    document, version = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(document)
    result = stack.search("Paragraph 3 sentence agreement", document, limit=4)
    ids = set(chunk_ids(stack, document, version))
    assert 0 < len(result.hits) <= 4
    assert [h.rank for h in result.hits] == list(range(1, len(result.hits) + 1))
    assert all(h.chunk_id.value in ids and h.version_number == 1 for h in result.hits)
    assert all(
        h.document_id.value == document and h.version_id.value == version for h in result.hits
    )
    scores = [h.score for h in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert all(not hasattr(h, "text") for h in result.hits)


def test_a_document_without_a_grant_is_never_searchable(stack: Stack, oracle: Engine) -> None:
    granted, _ = stack.upload(LONG_BODY.encode())
    secret, _ = stack.upload(LONG_BODY.replace("Paragraph", "Secret").encode())
    stack.drain()
    stack.grant(granted)  # no grant on `secret`
    hits = stack.search("Secret Paragraph agreement", granted).hits
    assert {h.document_id.value for h in hits} == {granted}
    assert secret not in {h.document_id.value for h in hits}
    assert (
        stack.access.resolve_access_scope.execute(
            stack.ctx(),
            AuthorizationRequest(ACTION, frozenset({ResourceRef(DOCUMENT_KIND, str(secret))})),
        ).scope
        is None
    )  # Access Control itself denies: there is nothing to search with


def test_a_proof_for_another_scope_is_refused_and_the_query_is_never_embedded(
    stack: Stack, oracle: Engine
) -> None:
    a, _ = stack.upload(LONG_BODY.encode())
    b, _ = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(a, b)
    narrow, wide = stack.decision(a), stack.decision(a, b)
    before = stack.embedder.query_calls
    with pytest.raises(AuthorizationError):
        stack.retrieval.search.execute(
            stack.ctx(), SearchQuery(wide.scope, narrow.proof.token, "agreement", 3)
        )
    assert stack.embedder.query_calls == before


def test_a_revoked_grant_makes_the_proof_stale_and_search_fails_closed(
    stack: Stack, oracle: Engine
) -> None:
    document, _ = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(document)
    decision = stack.decision(document)
    stack.access.revoke_access.execute(
        stack.ctx(),
        RevokeAccessCommand(stack.alice, ResourceRef(DOCUMENT_KIND, str(document)), ACTION),
    )
    with pytest.raises(AuthorizationError) as raised:
        stack.retrieval.search.execute(
            stack.ctx(), SearchQuery(decision.scope, decision.proof.token, "agreement", 3)
        )
    assert raised.value.code == "proof_stale_epoch"


def test_an_expired_proof_is_refused(stack: Stack, oracle: Engine) -> None:
    document, _ = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(document)
    decision = stack.decision(document)
    stack.clock.advance(timedelta(seconds=60))
    with pytest.raises(AuthorizationError) as raised:
        stack.retrieval.search.execute(
            stack.ctx(), SearchQuery(decision.scope, decision.proof.token, "agreement", 3)
        )
    assert raised.value.code == "proof_expired"


def test_a_new_version_supersedes_the_old_one_once_it_is_ready(
    stack: Stack, oracle: Engine
) -> None:
    document, v1 = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(document)
    assert {h.version_id.value for h in stack.search("Paragraph agreement", document).hits} == {v1}
    second = stack.docs.add_document_version.execute(
        stack.ctx(),
        AddDocumentVersionCommand(
            DocumentId(document), TEXT, LONG_BODY.replace("Paragraph", "Clause").encode()
        ),
    )
    v2 = second.version_id.value
    assert {h.version_id.value for h in stack.search("Paragraph agreement", document).hits} == {v1}
    stack.drain()  # v2 passes scan, extract, chunk, index and becomes ready
    assert version_status(oracle, v2) == "ready"
    hits = stack.search("Clause agreement", document).hits
    assert hits and {h.version_id.value for h in hits} == {v2}
    assert all(h.version_number == 2 for h in hits)
    assert scalar(oracle, "SELECT count(*) FROM retrieval.index_versions") == 2  # both recorded


def test_a_failed_extraction_is_never_indexed(stack: Stack, oracle: Engine) -> None:
    _, version = stack.upload(b" \n\t ")
    stack.drain()
    assert version_status(oracle, version) == "failed"
    assert scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors") == 0
    assert (
        scalar(oracle, "SELECT status FROM ingestion.stage_runs WHERE stage = 'index'") == "pending"
    )


def test_a_plan_with_index_before_chunk_fails_closed(database: Database, oracle: Engine) -> None:
    s = Stack(database, order=("extract", "index", "chunk"))
    try:
        _, version = s.upload()
        s.drain()
        assert version_status(oracle, version) == "failed"  # never ready, never searchable
        assert scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors") == 0
    finally:
        s.close()


def _dump(table: str) -> str:
    return f"SELECT coalesce(string_agg(r::text, ''), '') FROM {table} r"  # noqa: S608


def test_no_content_reaches_jobs_audit_or_any_retrieval_table(stack: Stack, oracle: Engine) -> None:
    document, _ = stack.upload()
    stack.drain()
    stack.grant(document)
    stack.search("TOP-SECRET indemnity", document)
    tables = (
        "platform.jobs",
        "audit.records",
        "retrieval.index_versions",
        "retrieval.chunk_vectors",
        "retrieval.purge_receipts",
        "ingestion.runs",
        "ingestion.stage_runs",
    )
    with oracle.connect() as conn:
        dumps = [str(conn.execute(text(_dump(table))).scalar_one()) for table in tables]
    assert all("TOP-SECRET" not in d and "Second paragraph" not in d for d in dumps)


def test_delete_and_purge_remove_the_vectors_and_leave_their_own_receipt(
    stack: Stack, oracle: Engine
) -> None:
    document, _ = stack.upload()
    stack.drain()
    stack.grant(document)
    assert int(str(scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors"))) >= 1
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document)))
    stack.drain()
    assert scalar(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors") == 0
    assert scalar(oracle, "SELECT count(*) FROM retrieval.index_versions") == 0
    assert scalar(oracle, "SELECT count(*) FROM retrieval.purge_receipts") == 1
    assert scalar(oracle, "SELECT scope FROM retrieval.purge_receipts") == "retrieval.vector_rows"
    assert scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0


def test_a_tombstoned_document_is_unreachable_before_the_purge_runs(
    stack: Stack, oracle: Engine
) -> None:
    document, _ = stack.upload(LONG_BODY.encode())
    stack.drain()
    stack.grant(document)
    decision = stack.decision(document)
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document)))
    assert (
        int(str(scalar(oracle, "SELECT count(*) FROM retrieval.chunk_vectors"))) >= 1
    )  # purge not run yet
    try:
        result = stack.retrieval.search.execute(
            stack.ctx(), SearchQuery(decision.scope, decision.proof.token, "agreement", 3)
        )
        assert result.hits == ()
    except AuthorizationError:
        pass  # deleting also revokes access: either answer is closed, never a hit


def test_tenants_are_isolated_end_to_end(stack: Stack, oracle: Engine) -> None:
    other = stack.identity.register_principal.execute(
        SystemContext(T2, "retrieval-e2e"), RegisterPrincipalCommand(PrincipalKind.USER, "Bob")
    ).principal_id
    doc1, ver1 = stack.upload(LONG_BODY.encode(), tenant=T1)
    view = stack.docs.upload_document.execute(
        TenantContext(T2, other, RequestId(UUID(int=10))),
        UploadDocumentCommand("Other", TEXT, LONG_BODY.encode()),
    )
    doc2, ver2 = view.document_id.value, view.version_id.value
    stack.drain()
    assert version_status(oracle, ver1) == version_status(oracle, ver2) == "ready"
    stack.grant(doc1)
    hits = stack.search("Paragraph agreement", doc1, limit=5).hits
    assert hits and {h.document_id.value for h in hits} == {doc1}
    assert not {h.chunk_id.value for h in hits} & set(chunk_ids(stack, doc2, ver2, T2))
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(doc1)))
    stack.drain()
    assert scalar(oracle, "SELECT count(*) FROM retrieval.index_versions") == 1  # T2's survives


def test_unsafe_retrieval_settings_fail_at_composition(stack: Stack) -> None:
    versions = build_version_state_reader(stack.platform, stack.access)
    for bad in (RetrievalSettings(candidate_pool=100, max_candidates=10),):
        with pytest.raises(ValueError):
            build_index(
                stack.platform,
                chunks=stack.read_chunks,
                versions=versions,
                verifier=stack.access.proof_verifier,
                embedder=stack.embedder,
                settings=bad,
            )
    with pytest.raises(ValueError):
        RetrievalSettings(max_results=0)
