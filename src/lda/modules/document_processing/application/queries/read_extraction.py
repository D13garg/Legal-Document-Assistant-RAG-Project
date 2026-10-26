"""`ReadExtraction`: the only way another module obtains the extracted text (ADR-038 §1)."""

from __future__ import annotations

from lda.modules.document_processing.application.dto import ReadExtractionCommand, StoredExtraction
from lda.modules.document_processing.application.ports import ProcessingUnitOfWorkFactory
from lda.modules.document_processing.domain.extraction import ExtractedDocument, content_hash
from lda.modules.document_processing.domain.resource import extracted_path
from lda.modules.documents.api import (
    DocumentId as SourceDocumentId,
)
from lda.modules.documents.api import (
    DocumentVersionId as SourceVersionId,
)
from lda.modules.documents.api import ReadVersionState, VersionStateQuery, VersionStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ConflictError, NotFoundError, ValidationError
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey

EXTRACTION_NOT_FOUND = "extraction_not_found"
EXTRACTION_ARTIFACT_MISSING = "extraction_artifact_missing"
EXTRACTION_VERSION_NOT_ELIGIBLE = "extraction_version_not_eligible"
EXTRACTION_ARTIFACT_CORRUPT = "extraction_artifact_corrupt"

ELIGIBLE_STATUSES = frozenset({VersionStatus.PENDING, VersionStatus.READY})
"""A version still moving through the pipeline, or one that completed it. A failed or quarantined
version never serves derived data."""


@public(reason="Runs only as pipeline work: later stages read the extraction of their version.")
@not_audited(reason="Read-only content access by the pipeline; reads are not audited (ADR-019 D2).")
class ReadExtraction:
    """Returns the recorded, verified extraction of a version, or refuses.

    Order of checks, each before the next is attempted:
    1. Documents (its public API, under the caller's tenant via RLS) must know the document as
       active and the version as belonging to it, and the version must be `pending` or `ready`;
    2. this module's own tenant-bound row must exist;
    3. the artifact is read only now, its SHA-256 must equal the recorded hash and its counts the
       recorded counts. The key layout never leaves this module (ADR-037 §3).
    A tombstoned document is refused at step 1, so nothing is served from the moment of deletion,
    whatever the state of the purge. The text is returned to a system caller only: serving it to a
    user or a prompt is another module's decision and needs an `AuthorizedContext` (ADR-014).
    """

    def __init__(
        self,
        units_of_work: ProcessingUnitOfWorkFactory,
        store: ObjectStore,
        versions: ReadVersionState,
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._versions = versions

    def execute(self, ctx: SystemContext, command: ReadExtractionCommand) -> StoredExtraction:
        state = self._versions.execute(
            ctx,
            VersionStateQuery(
                SourceDocumentId(command.document_id.value),
                SourceVersionId(command.version_id.value),
            ),
        )  # NotFoundError for a tombstoned, unknown, foreign or other-tenant document or version
        if state.status not in ELIGIBLE_STATUSES:
            raise ConflictError(EXTRACTION_VERSION_NOT_ELIGIBLE)
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            record = uow.extractions.get(command.version_id)
        if record is None or record.document_id != command.document_id:
            raise NotFoundError(EXTRACTION_NOT_FOUND)
        key = TenantScopedKey(ctx.tenant_id, extracted_path(record.document_id, record.version_id))
        try:
            artifact = self._store.get(key)
        except NotFoundError:
            raise NotFoundError(EXTRACTION_ARTIFACT_MISSING) from None
        if content_hash(artifact) != record.content_hash:
            raise ConflictError(EXTRACTION_ARTIFACT_CORRUPT)
        try:
            extracted = ExtractedDocument.from_bytes(artifact)
        except ValidationError:
            raise ConflictError(EXTRACTION_ARTIFACT_CORRUPT) from None
        if (len(extracted.blocks), extracted.character_count) != (
            record.block_count,
            record.character_count,
        ):
            raise ConflictError(EXTRACTION_ARTIFACT_CORRUPT)
        return StoredExtraction(
            record.document_id,
            record.version_id,
            record.parser_version,
            record.content_hash,
            extracted,
        )
