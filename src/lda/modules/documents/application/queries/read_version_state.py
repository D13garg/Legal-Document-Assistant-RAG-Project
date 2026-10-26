from __future__ import annotations

from lda.modules.documents.application.dto.documents import VersionStateQuery, VersionStateView
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.application.queries.read_original import (
    DOCUMENT_NOT_FOUND,
    VERSION_NOT_FOUND,
)
from lda.modules.documents.domain.document import DocumentStatus
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import NotFoundError


@public(reason="Runs only as pipeline work: derived-data modules check a version before reading.")
@not_audited(
    reason="Read-only lifecycle check by the pipeline; reads are not audited (ADR-019 D2)."
)
class ReadVersionState:
    """Tells a derived-data module whether a version of an active document exists, and its status.

    Documents owns the lifecycle (ADR-010 §1), so the module that wants to read derived data asks
    here instead of mirroring a status. The row check runs under the caller's tenant (RLS). A
    tombstoned document, an unknown version and a version of another document are all `not found`,
    exactly like `ReadOriginal`; the status itself is returned and the caller decides which states
    it accepts. No object key, no bytes and no title ever leave this use case (ADR-038).
    """

    def __init__(self, units_of_work: DocumentsUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: SystemContext, query: VersionStateQuery) -> VersionStateView:
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(query.document_id)
            if document is None or document.status is DocumentStatus.DELETED:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
            version = uow.versions.get(query.version_id)
            if version is None or version.document_id != document.id:
                raise NotFoundError(VERSION_NOT_FOUND)
            return VersionStateView(document.id, version.id, version.status, version.number)
