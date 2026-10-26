from __future__ import annotations

from lda.modules.document_processing.application.dto import PurgeCommand, PurgeView
from lda.modules.document_processing.application.ports import ProcessingUnitOfWorkFactory
from lda.modules.document_processing.domain.record import PurgeReceipt
from lda.modules.document_processing.domain.resource import extracted_path
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.ports.clock import Clock
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey

VERIFICATION_FAILED = "extraction_purge_verification_failed"
VERSION_SET_CHANGED = "extraction_purge_version_set_changed"


@public(reason="Runs only as the handler of Documents' DocumentPurged event (ADR-023, ADR-037).")
@not_audited(
    reason="Removes derived artifacts and identifiers; the receipt is the evidence and Documents "
    "audits the purge itself (ADR-019 D2)."
)
class PurgeExtractions:
    """Deletes this module's derived data of one purged document: the artifacts first, verified
    absent, then the rows and an insert-only receipt in one transaction (ADR-023).

    The receipt claims only this module's artifacts and rows (`PURGE_SCOPE`). Idempotent: a retry
    after a crash re-deletes, re-verifies and completes; a document with a receipt is left alone.
    """

    def __init__(
        self, units_of_work: ProcessingUnitOfWorkFactory, store: ObjectStore, clock: Clock
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock

    def execute(self, ctx: SystemContext, command: PurgeCommand) -> PurgeView:
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            if uow.receipts.get(command.document_id) is not None:
                return PurgeView(command.document_id, purged=False, versions_purged=0)
            seen = tuple(
                sorted(
                    (r.version_id for r in uow.extractions.list_for(command.document_id)),
                    key=str,
                )
            )
        keys = [
            TenantScopedKey(ctx.tenant_id, extracted_path(command.document_id, version_id))
            for version_id in seen
        ]
        for key in keys:
            self._store.delete(key)
        if any(self._store.exists(key) for key in keys):
            raise InfrastructureError(VERIFICATION_FAILED)
        with self._units_of_work.begin(ctx) as uow:
            current = {r.version_id for r in uow.extractions.list_for(command.document_id)}
            if current != set(seen):
                raise ConflictError(VERSION_SET_CHANGED)  # a stage recorded meanwhile: retried
            uow.extractions.remove_all_for(command.document_id)
            uow.receipts.add(
                PurgeReceipt(ctx.tenant_id, command.document_id, self._clock.now(), seen)
            )
            uow.commit()
            return PurgeView(command.document_id, purged=True, versions_purged=len(seen))
