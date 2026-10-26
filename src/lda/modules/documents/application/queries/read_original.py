from __future__ import annotations

from lda.modules.documents.application.dto.documents import OriginalContent, ReadOriginalCommand
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.document import DocumentStatus, VersionStatus
from lda.modules.documents.domain.resource import original_path
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ConflictError, InfrastructureError, NotFoundError
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey

DOCUMENT_NOT_FOUND = "document_not_found"
VERSION_NOT_FOUND = "document_version_not_found"
VERSION_NOT_PENDING = "document_version_not_pending"
ORIGINAL_MISSING = "document_original_missing"
ORIGINAL_SIZE_MISMATCH = "document_original_size_mismatch"


@public(reason="Runs only as pipeline work for a pending version (ADR-010 §13, ADR-037).")
@not_audited(reason="Read-only content access by the pipeline; reads are not audited (ADR-019 D2).")
class ReadOriginal:
    """Gives the ingestion pipeline the original bytes of a version that is still `pending`.

    Documents keeps the object-key layout private (ADR-005): callers name a document and a version,
    never a key. The rows are checked first, under the caller's tenant (RLS), and the bytes are read
    only after that transaction ended, so no database transaction is held during object I/O. A
    tombstoned document, a version of another document, and any version that already left `pending`
    are refused: this is not a general content read path. The stored size must equal the recorded
    size, which catches a truncated or substituted object.
    """

    def __init__(self, units_of_work: DocumentsUnitOfWorkFactory, store: ObjectStore) -> None:
        self._units_of_work = units_of_work
        self._store = store

    def execute(self, ctx: SystemContext, command: ReadOriginalCommand) -> OriginalContent:
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(command.document_id)
            if document is None or document.status is DocumentStatus.DELETED:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
            version = uow.versions.get(command.version_id)
            if version is None or version.document_id != document.id:
                raise NotFoundError(VERSION_NOT_FOUND)
            if version.status is not VersionStatus.PENDING:
                raise ConflictError(VERSION_NOT_PENDING)
        key = TenantScopedKey(ctx.tenant_id, original_path(version.document_id, version.id))
        try:
            body = self._store.get(key)
        except NotFoundError:
            raise NotFoundError(ORIGINAL_MISSING) from None
        if len(body) != version.size_bytes:
            raise InfrastructureError(ORIGINAL_SIZE_MISMATCH)
        return OriginalContent(
            version.document_id, version.id, version.media_type, version.size_bytes, body
        )
