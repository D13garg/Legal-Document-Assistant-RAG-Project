from __future__ import annotations

from lda.modules.documents.application.attribution import attribution_of
from lda.modules.documents.application.dto.documents import DeleteView, DocumentReference
from lda.modules.documents.application.jobs import DOCUMENTS_QUEUE, PURGE_JOB
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.events import DocumentDeleted
from lda.modules.documents.domain.resource import DOCUMENT_KIND
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import NotFoundError
from lda.shared.kernel.ids import EventId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec


@requires("documents.delete_document")
@audited("documents.document_deleted")
class DeleteDocument:
    """Tombstone and revocation of every grant are ONE transaction (invariant 12, ADR-023)."""

    def __init__(
        self, units_of_work: DocumentsUnitOfWorkFactory, clock: Clock, ids: IdGenerator
    ) -> None:
        self._units_of_work = units_of_work
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: ExecutionContext, command: DocumentReference) -> DeleteView:
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(command.document_id, for_update=True)
            if document is None:
                raise NotFoundError("document_not_found")
            now = self._clock.now()
            if not document.tombstone(now):
                return DeleteView(document.id, changed=False, revoked_grants=0)
            uow.documents.save(document)
            revoked = uow.access_revoker.revoke_all(DOCUMENT_KIND, str(document.id))
            uow.jobs.enqueue(
                JobSpec(
                    queue=DOCUMENTS_QUEUE,
                    job_type=PURGE_JOB,
                    payload={"document_id": str(document.id)},
                    unique_key=f"{PURGE_JOB}:{document.id}",
                )
            )
            uow.events.publish(
                DocumentDeleted(
                    event_id=EventId(self._ids.new_uuid()),
                    tenant_id=ctx.tenant_id,
                    occurred_at=now,
                    document_id=document.id,
                ),
                Delivery.ASYNC_JOB,
            )
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="documents.document_deleted",
                    resource_type="document",
                    resource_id=str(document.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=now,
                    attributes={**who.attributes, "revoked_grants": str(revoked)},
                )
            )
            uow.commit()
            return DeleteView(document.id, changed=True, revoked_grants=revoked)
