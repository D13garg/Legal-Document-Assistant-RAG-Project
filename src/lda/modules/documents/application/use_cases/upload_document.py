from __future__ import annotations

from lda.modules.documents.application.attribution import attribution_of
from lda.modules.documents.application.dto.documents import UploadDocumentCommand, UploadedView
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.application.upload_staging import UploadStaging
from lda.modules.documents.domain.document import Document, clean_title
from lda.modules.documents.domain.events import DocumentVersionAdded
from lda.modules.documents.domain.ids import DocumentId
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, requires
from lda.shared.kernel.errors import ConflictError
from lda.shared.kernel.ids import EventId
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.events import Delivery


@requires("documents.upload_document")
@audited("documents.document_uploaded")
class UploadDocument:
    """Creates a document and its first version: intent, then object, then rows (ADR-005)."""

    def __init__(
        self,
        units_of_work: DocumentsUnitOfWorkFactory,
        staging: UploadStaging,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._units_of_work = units_of_work
        self._staging = staging
        self._clock = clock
        self._ids = ids

    def execute(self, ctx: ExecutionContext, command: UploadDocumentCommand) -> UploadedView:
        title = clean_title(command.title)  # before any write
        document_id = DocumentId(self._ids.new_uuid())
        staged = self._staging.stage(ctx, document_id, command.media_type, command.body)
        try:
            with self._units_of_work.begin(ctx) as uow:
                if uow.intents.get(staged.version_id, for_update=True) is None:
                    raise ConflictError("upload_expired")
                now = self._clock.now()
                who = attribution_of(ctx)
                document, version = Document.create(
                    document_id=document_id,
                    version_id=staged.version_id,
                    tenant_id=ctx.tenant_id,
                    title=title,
                    media_type=staged.media_type,
                    size_bytes=staged.size_bytes,
                    created_by=who.actor,
                    now=now,
                )
                uow.documents.add(document)
                uow.versions.add(version)
                uow.intents.remove(staged.version_id)
                uow.audit.record(
                    AuditFact(
                        tenant_id=ctx.tenant_id,
                        actor=who.actor,
                        action="documents.document_uploaded",
                        resource_type="document",
                        resource_id=str(document_id),
                        outcome=AuditOutcome.SUCCESS,
                        request_id=who.request_id,
                        occurred_at=now,
                        attributes={
                            **who.attributes,
                            "version_id": str(version.id),
                            "version_number": str(version.number),
                            "media_type": version.media_type,
                            "size_bytes": str(version.size_bytes),
                        },
                    )
                )
                uow.events.publish(
                    DocumentVersionAdded(
                        event_id=EventId(self._ids.new_uuid()),
                        tenant_id=ctx.tenant_id,
                        occurred_at=now,
                        document_id=document_id,
                        version_id=version.id,
                        version_number=version.number,
                    ),
                    Delivery.ASYNC_JOB,
                )
                uow.commit()
        except Exception:
            self._staging.discard(staged)
            raise
        return UploadedView(document_id, version.id, version.number, version.status.value)
