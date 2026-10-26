"""The `extract` pipeline stage (ADR-010 §4, ADR-037): original bytes in, immutable artifact out.

Order of one run, built so that a crash at any point is repaired by running the stage again:
1. read the original through Documents (refused unless the version is still `pending`);
2. extract and normalize it (a content failure is permanent, never retried);
3. in ONE fenced transaction record the extraction (identifiers, hash, counts: never the text);
4. write the artifact with `overwrite=False`; an existing identical object is adopted.
A row without its object is therefore the only intermediate state, it is findable by purge, and a
re-run completes it. Recorded output is never replaced, whatever a later run produces.
"""

from __future__ import annotations

from lda.modules.document_processing.application.ports import (
    ContentParser,
    ProcessingUnitOfWorkFactory,
)
from lda.modules.document_processing.domain.extraction import ExtractionLimits, content_hash
from lda.modules.document_processing.domain.failures import (
    ARTIFACT_CONFLICT,
    ARTIFACT_UNRECOVERABLE,
    ORIGINAL_MISSING,
    VERSION_UNAVAILABLE,
    ExtractionRejected,
)
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import (
    ExtractionRecord,
    Reconciliation,
    reconcile,
)
from lda.modules.document_processing.domain.resource import extracted_path
from lda.modules.documents.api import (
    ORIGINAL_MISSING as DOCUMENTS_ORIGINAL_MISSING,
)
from lda.modules.documents.api import (
    DocumentId as SourceDocumentId,
)
from lda.modules.documents.api import (
    DocumentVersionId as SourceVersionId,
)
from lda.modules.documents.api import ReadOriginal, ReadOriginalCommand
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

STAGE_NAME = "extract"
PURPOSE = "document_processing:extract"
ARTIFACT_CONTENT_TYPE = "application/json"
RECORD_VANISHED = "extraction_record_vanished"


class ExtractStage:
    def __init__(
        self,
        reader: ReadOriginal,
        parser: ContentParser,
        units_of_work: ProcessingUnitOfWorkFactory,
        store: ObjectStore,
        clock: Clock,
        limits: ExtractionLimits,
    ) -> None:
        self._reader = reader
        self._parser = parser
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock
        self._limits = limits

    @property
    def name(self) -> str:
        return STAGE_NAME

    def run(self, request: StageRequest) -> StageOutcome:
        ctx = SystemContext(request.tenant_id, PURPOSE, request.job_id)
        document_id = DocumentId(request.document.value)
        version_id = VersionId(request.version.value)
        try:
            original = self._reader.execute(
                ctx,
                ReadOriginalCommand(
                    SourceDocumentId(request.document.value), SourceVersionId(request.version.value)
                ),
            )
        except NotFoundError as exc:
            missing = exc.code == DOCUMENTS_ORIGINAL_MISSING
            return StagePermanentFailure(ORIGINAL_MISSING if missing else VERSION_UNAVAILABLE)
        except ConflictError:
            return StagePermanentFailure(VERSION_UNAVAILABLE)  # no longer pending
        # Any other failure (storage, database) propagates: the engine retries it.
        try:
            extracted = self._parser.parse(original.media_type, original.body, self._limits)
        except ExtractionRejected as exc:
            return StagePermanentFailure(exc.code)
        produced, artifact = ExtractionRecord.describe(
            tenant_id=request.tenant_id,
            document_id=document_id,
            version_id=version_id,
            parser_version=self._parser.version,
            extracted=extracted,
            now=self._clock.now(),
        )
        recorded, decision = self._record(ctx, request, produced)
        if decision is Reconciliation.CONFLICT:
            return StagePermanentFailure(ARTIFACT_CONFLICT)
        key = TenantScopedKey(request.tenant_id, extracted_path(document_id, version_id))
        if decision is Reconciliation.ADOPT:
            return self._store_artifact(key, artifact, recorded.content_hash)
        return self._verify_recorded(key, recorded.content_hash)

    def _record(
        self, ctx: SystemContext, request: StageRequest, produced: ExtractionRecord
    ) -> tuple[ExtractionRecord, Reconciliation]:
        """Insert-if-absent under the fence: a stale executor cannot record anything."""
        with self._units_of_work.begin(ctx) as uow:
            uow.fence.verify(request.job_id, request.fencing_token)
            if uow.extractions.add(produced):
                uow.commit()
                return produced, Reconciliation.ADOPT
            recorded = uow.extractions.get(produced.version_id)
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
