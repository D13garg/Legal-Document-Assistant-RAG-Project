"""`ReadChunks`: the only way another module obtains chunk text and chunk ids (ADR-038, Step 11)."""

from __future__ import annotations

from lda.modules.chunking.application.ports import ChunkingUnitOfWorkFactory
from lda.modules.chunking.application.read_dto import ChunkView, ReadChunksCommand, StoredChunks
from lda.modules.chunking.domain.artifact import chunk_set_from_artifact
from lda.modules.chunking.domain.chunk import content_hash
from lda.modules.chunking.domain.resource import chunks_path
from lda.modules.documents.api import DocumentId as SourceDocumentId
from lda.modules.documents.api import DocumentVersionId as SourceVersionId
from lda.modules.documents.api import ReadVersionState, VersionStateQuery, VersionStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ConflictError, NotFoundError, ValidationError
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey

CHUNKS_NOT_FOUND = "chunks_not_found"
CHUNKS_ARTIFACT_MISSING = "chunks_artifact_missing"
CHUNKS_VERSION_NOT_ELIGIBLE = "chunks_version_not_eligible"
CHUNKS_ARTIFACT_CORRUPT = "chunks_artifact_corrupt"

ELIGIBLE_STATUSES = frozenset({VersionStatus.PENDING, VersionStatus.READY})
"""A version still moving through the pipeline (the `index` stage runs while it is pending), or
one that completed it. A failed or quarantined version never serves derived data."""


@public(
    reason="Runs only as pipeline or retrieval work: derived-data modules read chunks (ADR-008)."
)
@not_audited(reason="Read-only content access by the system; reads are not audited (ADR-019 D2).")
class ReadChunks:
    """Returns the recorded, verified chunk set of a version, or refuses.

    Order of checks, each before the next is attempted:
    1. Documents (its public API, under the caller's tenant via RLS) must know the document as
       active and the version as belonging to it, and the version must be `pending` or `ready`;
    2. this module's own tenant-bound row must exist;
    3. the artifact is read only now; its SHA-256 must equal the recorded hash and it must parse
       and round-trip to exactly the same bytes (so every chunk id is one this module minted).
    A tombstoned document is refused at step 1. The purge removes the artifact before the row and
    the row before the receipt, so a read either sees a complete set, finds the row gone, or finds
    the artifact missing: never partial text. The key layout never leaves this module. It takes no
    lifecycle lock: it writes nothing, and a lock would only make a purge wait for a reader.
    """

    def __init__(
        self,
        units_of_work: ChunkingUnitOfWorkFactory,
        store: ObjectStore,
        versions: ReadVersionState,
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._versions = versions

    def execute(self, ctx: SystemContext, command: ReadChunksCommand) -> StoredChunks:
        state = self._versions.execute(
            ctx,
            VersionStateQuery(
                SourceDocumentId(command.document_id.value),
                SourceVersionId(command.version_id.value),
            ),
        )  # NotFoundError for a tombstoned, unknown, foreign or other-tenant document or version
        if state.status not in ELIGIBLE_STATUSES:
            raise ConflictError(CHUNKS_VERSION_NOT_ELIGIBLE)
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            record = uow.chunk_sets.get(command.version_id)
        if record is None or record.document_id != command.document_id:
            raise NotFoundError(CHUNKS_NOT_FOUND)
        key = TenantScopedKey(ctx.tenant_id, chunks_path(record.document_id, record.version_id))
        try:
            artifact = self._store.get(key)
        except NotFoundError:
            raise NotFoundError(CHUNKS_ARTIFACT_MISSING) from None
        if content_hash(artifact) != record.content_hash:
            raise ConflictError(CHUNKS_ARTIFACT_CORRUPT)
        try:
            chunk_set = chunk_set_from_artifact(artifact, record)
        except ValidationError:
            raise ConflictError(CHUNKS_ARTIFACT_CORRUPT) from None
        if (len(chunk_set.chunks), chunk_set.character_count) != (
            record.chunk_count,
            record.character_count,
        ):
            raise ConflictError(CHUNKS_ARTIFACT_CORRUPT)
        return StoredChunks(
            record.document_id,
            record.version_id,
            record.extraction_hash,
            record.chunker_version,
            record.content_hash,
            tuple(
                ChunkView(
                    chunk_set.chunk_id(c.ordinal),
                    c.ordinal,
                    c.text,
                    c.text_hash,
                    c.section_path,
                    c.page_start,
                    c.page_end,
                )
                for c in chunk_set.chunks
            ),
        )
