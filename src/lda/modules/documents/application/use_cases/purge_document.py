from __future__ import annotations

from lda.modules.documents.application.attribution import attribution_of
from lda.modules.documents.application.dto.documents import DocumentReference, PurgeView
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.document import DocumentStatus
from lda.modules.documents.domain.events import DocumentPurged
from lda.modules.documents.domain.evidence import PurgeReceipt
from lda.modules.documents.domain.resource import original_path
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, public
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import EventId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import Delivery
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey


@public(reason="Runs only as the system job enqueued by DeleteDocument; never exposed to a caller.")
@audited("documents.document_purged")
class PurgeDocument:
    """Deletes the originals, verifies they are gone, then removes the rows and writes the receipt.

    The receipt proves only that (ADR-023): downstream modules add their own purge and receipts.
    Idempotent: a retry after a crash re-deletes, re-verifies and completes.
    """

    def __init__(
        self,
        units_of_work: DocumentsUnitOfWorkFactory,
        store: ObjectStore,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: ExecutionContext, command: DocumentReference) -> PurgeView:
        with self._units_of_work.begin(ctx) as uow:  # read: nothing to commit
            receipt = uow.receipts.get(command.document_id)
            if receipt is not None:
                return PurgeView(command.document_id, purged=False, versions_purged=0)
            document = uow.documents.get(command.document_id)
            if document is None:
                return PurgeView(command.document_id, purged=False, versions_purged=0)
            if document.status is not DocumentStatus.DELETED:
                raise ConflictError("document_not_deleted")
            seen = {v.id for v in uow.versions.list_for(command.document_id)}
        keys = [
            TenantScopedKey(ctx.tenant_id, original_path(command.document_id, version_id))
            for version_id in sorted(seen, key=str)
        ]
        for key in keys:
            self._store.delete(key)
        if any(self._store.exists(key) for key in keys):
            raise InfrastructureError("purge_verification_failed")
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(command.document_id, for_update=True)
            if document is None or document.deleted_at is None:
                return PurgeView(command.document_id, purged=False, versions_purged=0)
            versions = uow.versions.list_for(command.document_id)
            if {v.id for v in versions} != seen:
                raise ConflictError("purge_version_set_changed")
            now = self._clock.now()
            uow.versions.remove_all_for(command.document_id)
            uow.documents.remove(command.document_id)
            uow.receipts.add(
                PurgeReceipt(
                    ctx.tenant_id,
                    command.document_id,
                    document.deleted_at,
                    now,
                    tuple(v.id for v in versions),
                )
            )
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="documents.document_purged",
                    resource_type="document",
                    resource_id=str(command.document_id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=now,
                    attributes={**who.attributes, "versions_purged": str(len(versions))},
                )
            )
            uow.events.publish(
                DocumentPurged(
                    event_id=EventId(self._ids.new_uuid()),
                    tenant_id=ctx.tenant_id,
                    occurred_at=now,
                    document_id=command.document_id,
                    versions_purged=len(versions),
                ),
                Delivery.ASYNC_JOB,
            )
            uow.commit()
            return PurgeView(command.document_id, purged=True, versions_purged=len(versions))
