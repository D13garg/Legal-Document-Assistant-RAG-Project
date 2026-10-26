"""The `index` pipeline stage (ADR-010 §4, ADR-008): chunk text in, vectors out.

Order of one run, built so that a crash at any point is repaired by running the stage again:
1. read the verified chunk set through `chunking.api` (which asks Documents first);
2. if this version already has a record for this embedding identity, compare and finish (a
   duplicate delivery of the job does nothing twice);
3. embed outside any transaction (the slow part holds no lock and no connection);
4. in ONE fenced transaction, under the document's lifecycle lock, refuse if the document was
   already purged, else insert the version record and every vector together.
There is no intermediate state: the record and its vectors commit or fail as one. A recorded
version is never replaced, whatever a later run would produce.
"""

from __future__ import annotations

from collections.abc import Sequence

from lda.modules.ingestion.api import (
    StageOutcome,
    StagePermanentFailure,
    StageRequest,
    StageSucceeded,
)
from lda.modules.retrieval.application.ports import (
    ChunkSource,
    Embedder,
    RetrievalUnitOfWorkFactory,
)
from lda.modules.retrieval.application.source import SourceChunks
from lda.modules.retrieval.domain.embedding import checked_vector
from lda.modules.retrieval.domain.failures import (
    EMBEDDING_INVALID,
    INDEX_CONFLICT,
    VERSION_UNAVAILABLE,
    RetrievalRejected,
)
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.domain.record import IndexedChunk, IndexedVersion
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import InfrastructureError, ValidationError
from lda.shared.ports.clock import Clock

STAGE_NAME = "index"
PURPOSE = "retrieval:index"
RECORD_VANISHED = "index_record_vanished"


class IndexStage:
    def __init__(
        self,
        chunks: ChunkSource,
        embedder: Embedder,
        units_of_work: RetrievalUnitOfWorkFactory,
        clock: Clock,
        batch_size: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self._chunks = chunks
        self._embedder = embedder
        self._units_of_work = units_of_work
        self._clock = clock
        self._batch_size = batch_size

    @property
    def name(self) -> str:
        return STAGE_NAME

    def run(self, request: StageRequest) -> StageOutcome:
        ctx = SystemContext(request.tenant_id, PURPOSE, request.job_id)
        document_id = DocumentId(request.document.value)
        version_id = VersionId(request.version.value)
        identity = self._embedder.identity
        try:
            source = self._chunks.read(ctx, document_id, version_id)
        except RetrievalRejected as exc:
            return StagePermanentFailure(exc.code)
        # Any other failure (storage, database) propagates: the engine retries it.
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            recorded = uow.index.get_version(version_id, identity.index_id)
        if recorded is not None:
            return self._compare(recorded, source)
        try:
            vectors = self._embed(source)
        except ValidationError:
            return StagePermanentFailure(EMBEDDING_INVALID)
        produced = IndexedVersion(
            request.tenant_id,
            document_id,
            version_id,
            identity,
            source.chunker_version,
            source.content_hash,
            len(source.chunks),
            self._clock.now(),
        )
        entries = [
            IndexedChunk(c.chunk_id, c.ordinal, c.text_hash, v)
            for c, v in zip(source.chunks, vectors, strict=True)
        ]
        try:
            return self._record(ctx, request, produced, entries)
        except RetrievalRejected as exc:  # the document was purged: nothing may be added
            return StagePermanentFailure(exc.code)

    def _embed(self, source: SourceChunks) -> list[tuple[float, ...]]:
        out: list[tuple[float, ...]] = []
        texts = [c.text for c in source.chunks]
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            result = self._embedder.embed_documents(batch)
            if len(result) != len(batch):
                raise ValidationError(EMBEDDING_INVALID)
            out.extend(checked_vector(v) for v in result)
        return out

    def _record(
        self,
        ctx: SystemContext,
        request: StageRequest,
        produced: IndexedVersion,
        entries: Sequence[IndexedChunk],
    ) -> StageOutcome:
        """Insert-if-absent under the fence and the lifecycle lock: a stale executor cannot record
        anything, and nothing is recorded for a document whose purge already finished."""
        with self._units_of_work.begin(ctx) as uow:
            uow.fence.verify(request.job_id, request.fencing_token)
            uow.index.lock_document(produced.document_id)
            if uow.receipts.get(produced.document_id) is not None:
                raise RetrievalRejected(VERSION_UNAVAILABLE)
            if uow.index.add_version(produced, entries):
                uow.commit()
                return StageSucceeded()
            recorded = uow.index.get_version(produced.version_id, produced.index_id)
            if recorded is None:
                raise InfrastructureError(RECORD_VANISHED)  # retried
            return self._compare(recorded, produced)

    @staticmethod
    def _compare(recorded: IndexedVersion, produced: IndexedVersion | SourceChunks) -> StageOutcome:
        """Recorded output is immutable: the same chunk set succeeds, anything else conflicts."""
        if isinstance(produced, SourceChunks):
            same = (recorded.source_hash, recorded.chunk_count) == (
                produced.content_hash,
                len(produced.chunks),
            )
        else:
            same = (recorded.source_hash, recorded.chunk_count) == (
                produced.source_hash,
                produced.chunk_count,
            )
        return StageSucceeded() if same else StagePermanentFailure(INDEX_CONFLICT)
