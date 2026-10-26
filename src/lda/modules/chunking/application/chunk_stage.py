"""The `chunk` pipeline stage (ADR-010 §4, ADR-038): extracted text in, immutable chunk set out.

Order of one run, built so that a crash at any point is repaired by running the stage again:
1. read the verified extraction through `document_processing.api` (which asks Documents first);
2. chunk it (pure and deterministic; a content failure is permanent, never retried);
3. in ONE fenced transaction, under the document's lifecycle lock, refuse if the document was
   already purged, else record the chunk set (identifiers, hashes, counts: never the text);
4. write the artifact with `overwrite=False`; an existing identical object is adopted;
5. confirm, under the same lock, that no purge completed meanwhile; if it did, remove the object.
A row without its object is the only intermediate state; it is findable by purge, and a re-run
completes it. Recorded output is never replaced, whatever a later run produces.
"""

from __future__ import annotations

from lda.modules.chunking.application.ports import ChunkingUnitOfWorkFactory
from lda.modules.chunking.domain.chunk import ChunkSet, content_hash
from lda.modules.chunking.domain.chunker import ChunkingParameters, SourceBlock, chunk_blocks
from lda.modules.chunking.domain.failures import (
    ARTIFACT_CONFLICT,
    ARTIFACT_UNRECOVERABLE,
    EXTRACTION_CORRUPT,
    EXTRACTION_UNAVAILABLE,
    VERSION_UNAVAILABLE,
    ChunkingRejected,
)
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import ChunkSetRecord, Reconciliation, reconcile
from lda.modules.chunking.domain.resource import chunks_path
from lda.modules.document_processing.api import (
    EXTRACTION_ARTIFACT_CORRUPT,
    EXTRACTION_ARTIFACT_MISSING,
    EXTRACTION_NOT_FOUND,
    ReadExtraction,
    ReadExtractionCommand,
    StoredExtraction,
)
from lda.modules.document_processing.api import DocumentId as SourceDocumentId
from lda.modules.document_processing.api import VersionId as SourceVersionId
from lda.modules.ingestion.api import (
    StageOutcome,
    StagePermanentFailure,
    StageRequest,
    StageSucceeded,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.clock import Clock
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey

STAGE_NAME = "chunk"
PURPOSE = "chunking:chunk"
ARTIFACT_CONTENT_TYPE = "application/json"
RECORD_VANISHED = "chunk_record_vanished"
UNAVAILABLE_EXTRACTION_CODES = frozenset({EXTRACTION_NOT_FOUND, EXTRACTION_ARTIFACT_MISSING})


class ChunkStage:
    def __init__(
        self,
        reader: ReadExtraction,
        parameters: ChunkingParameters,
        units_of_work: ChunkingUnitOfWorkFactory,
        store: ObjectStore,
        clock: Clock,
    ) -> None:
        self._reader = reader
        self._parameters = parameters
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock

    @property
    def name(self) -> str:
        return STAGE_NAME

    def run(self, request: StageRequest) -> StageOutcome:
        ctx = SystemContext(request.tenant_id, PURPOSE, request.job_id)
        document_id = DocumentId(request.document.value)
        version_id = VersionId(request.version.value)
        key = TenantScopedKey(request.tenant_id, chunks_path(document_id, version_id))
        try:
            stored = self._reader.execute(
                ctx,
                ReadExtractionCommand(
                    SourceDocumentId(request.document.value), SourceVersionId(request.version.value)
                ),
            )
        except (NotFoundError, ConflictError) as exc:
            failure = _classify(exc)
            if failure == VERSION_UNAVAILABLE:
                self._discard_if_purged(ctx, document_id, key)  # a failed earlier cleanup
            return StagePermanentFailure(failure)
        # Any other failure (storage, database) propagates: the engine retries it.
        try:
            chunks = chunk_blocks(_source_blocks(stored), self._parameters)
        except ChunkingRejected as exc:
            return StagePermanentFailure(exc.code)
        produced, artifact = ChunkSetRecord.describe(
            ChunkSet(
                request.tenant_id,
                document_id,
                version_id,
                stored.content_hash,
                self._parameters.version,
                chunks,
            ),
            self._clock.now(),
        )
        try:
            recorded, decision = self._record(ctx, request, produced)
        except ChunkingRejected as exc:  # the document was purged: nothing may be added
            return StagePermanentFailure(exc.code)
        if decision is Reconciliation.CONFLICT:
            return StagePermanentFailure(ARTIFACT_CONFLICT)
        if decision is Reconciliation.ADOPT:
            outcome = self._store_artifact(key, artifact, recorded.content_hash)
        else:
            outcome = self._verify_recorded(key, recorded.content_hash)
        if isinstance(outcome, StageSucceeded) and self._discard_if_purged(ctx, document_id, key):
            return StagePermanentFailure(VERSION_UNAVAILABLE)
        return outcome

    def _record(
        self, ctx: SystemContext, request: StageRequest, produced: ChunkSetRecord
    ) -> tuple[ChunkSetRecord, Reconciliation]:
        """Insert-if-absent under the fence and the lifecycle lock: a stale executor cannot record
        anything, and nothing is recorded for a document whose purge already finished."""
        with self._units_of_work.begin(ctx) as uow:
            uow.fence.verify(request.job_id, request.fencing_token)
            uow.chunk_sets.lock_document(produced.document_id)
            if uow.receipts.get(produced.document_id) is not None:
                raise ChunkingRejected(VERSION_UNAVAILABLE)
            if uow.chunk_sets.add(produced):
                uow.commit()
                return produced, Reconciliation.ADOPT
            recorded = uow.chunk_sets.get(produced.version_id)
            if recorded is None:
                raise InfrastructureError(RECORD_VANISHED)  # retried
            return recorded, reconcile(recorded, produced)

    def _store_artifact(self, key: TenantScopedKey, artifact: bytes, expected: str) -> StageOutcome:
        try:
            self._store.put(key, artifact, content_type=ARTIFACT_CONTENT_TYPE, overwrite=False)
        except ConflictError:
            return self._verify_recorded(key, expected)  # a crashed run already wrote it
        return StageSucceeded()

    def _verify_recorded(self, key: TenantScopedKey, expected: str) -> StageOutcome:
        """The object under a recorded hash must exist and still match it. Never overwritten."""
        try:
            stored = self._store.get(key)
        except NotFoundError:
            return StagePermanentFailure(ARTIFACT_UNRECOVERABLE)
        if content_hash(stored) != expected:
            return StagePermanentFailure(ARTIFACT_CONFLICT)
        return StageSucceeded()

    def _discard_if_purged(
        self, ctx: SystemContext, document_id: DocumentId, key: TenantScopedKey
    ) -> bool:
        """After a purge receipt exists no row can be added, so an object under this key is an
        orphan (this run's late write, or a failed earlier cleanup): remove it. The lock makes the
        receipt read definitive: the purge either finished before it or will see the record."""
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            uow.chunk_sets.lock_document(document_id)
            purged = uow.receipts.get(document_id) is not None
        if purged:
            self._store.delete(key)
        return purged


def _classify(exc: NotFoundError | ConflictError) -> str:
    if isinstance(exc, NotFoundError) and exc.code in UNAVAILABLE_EXTRACTION_CODES:
        return EXTRACTION_UNAVAILABLE
    if isinstance(exc, ConflictError) and exc.code == EXTRACTION_ARTIFACT_CORRUPT:
        return EXTRACTION_CORRUPT
    return VERSION_UNAVAILABLE  # document gone, version unknown or foreign, not eligible


def _source_blocks(stored: StoredExtraction) -> list[SourceBlock]:
    return [SourceBlock(b.text, b.level, b.page) for b in stored.extracted.blocks]
