from __future__ import annotations

from lda.modules.documents.application.dto.documents import (
    DocumentReference,
    PurgeVerificationView,
)
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.resource import original_path
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, requires
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey


@requires("documents.verify_purge")
@not_audited(reason="Read-only verification of a purge; reads are not audited (ADR-019 D2).")
class VerifyDocumentPurge:
    """Checks only Documents' own data against the receipt (ADR-023): the receipt exists, no
    Documents row remains and every recorded original is absent. Nothing about other modules."""

    def __init__(self, units_of_work: DocumentsUnitOfWorkFactory, store: ObjectStore) -> None:
        self._units_of_work = units_of_work
        self._store = store

    def execute(self, ctx: ExecutionContext, query: DocumentReference) -> PurgeVerificationView:
        with self._units_of_work.begin(ctx) as uow:
            receipt = uow.receipts.get(query.document_id)
            rows_absent = uow.documents.get(
                query.document_id
            ) is None and not uow.versions.list_for(query.document_id)
        if receipt is None:
            return PurgeVerificationView(
                query.document_id,
                receipt_found=False,
                rows_absent=rows_absent,
                originals_absent=False,
            )
        originals_absent = not any(
            self._store.exists(TenantScopedKey(ctx.tenant_id, original_path(query.document_id, v)))
            for v in receipt.version_ids
        )
        return PurgeVerificationView(
            query.document_id,
            receipt_found=True,
            rows_absent=rows_absent,
            originals_absent=originals_absent,
        )
