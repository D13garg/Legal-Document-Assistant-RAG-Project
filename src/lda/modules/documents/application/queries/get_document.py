from __future__ import annotations

from lda.modules.documents.application.dto.documents import DocumentReference, DocumentView
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.document import DocumentStatus
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires
from lda.shared.kernel.errors import NotFoundError


@requires("documents.read_document")
@not_audited(reason="Read-only metadata query; reads are not audited in Step 4 (ADR-019 D2).")
class GetDocument:
    """A tombstoned document is gone: it is reported as not found."""

    def __init__(self, units_of_work: DocumentsUnitOfWorkFactory) -> None:
        self._units_of_work = units_of_work

    def execute(self, ctx: ExecutionContext, query: DocumentReference) -> DocumentView:
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(query.document_id)
            if document is None or document.status is DocumentStatus.DELETED:
                raise NotFoundError("document_not_found")
            return DocumentView.of(document, uow.versions.list_for(query.document_id))
