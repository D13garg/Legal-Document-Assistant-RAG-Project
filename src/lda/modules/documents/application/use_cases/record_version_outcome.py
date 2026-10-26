from __future__ import annotations

from lda.modules.documents.application.attribution import attribution_of
from lda.modules.documents.application.dto.documents import (
    OutcomeResult,
    OutcomeView,
    RecordOutcomeCommand,
)
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.document import DocumentStatus, VersionStatus
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import audited, public
from lda.shared.kernel.errors import ValidationError
from lda.shared.ports.audit import AuditFact, AuditOutcome
from lda.shared.ports.clock import Clock


@public(reason="Runs only as the handler of the pipeline's outcome events (ADR-010 §7).")
@audited("documents.version_outcome_recorded")
class RecordVersionOutcome:
    """Applies `pending -> ready | failed | quarantined`: Documents stays the authority over its own
    version status (ADR-015 amendment). Idempotent and fail-closed: a version that is not `pending`,
    a tombstoned document, or an id pair that does not belong together is never changed.
    """

    def __init__(self, units_of_work: DocumentsUnitOfWorkFactory, clock: Clock) -> None:
        self._units_of_work = units_of_work
        self._clock = clock

    def execute(self, ctx: ExecutionContext, command: RecordOutcomeCommand) -> OutcomeView:
        if command.outcome is VersionStatus.PENDING:
            raise ValidationError("document_outcome_invalid")  # `pending` is not an outcome
        with self._units_of_work.begin(ctx) as uow:
            document = uow.documents.get(command.document_id, for_update=True)
            version = uow.versions.get(command.version_id)
            if document is None or version is None or version.document_id != document.id:
                return OutcomeView(OutcomeResult.MISSING)
            if document.status is DocumentStatus.DELETED:
                return OutcomeView(OutcomeResult.DELETED)
            if version.status is command.outcome:
                return OutcomeView(OutcomeResult.ALREADY)
            if version.status is not VersionStatus.PENDING:
                return OutcomeView(OutcomeResult.REFUSED)  # another terminal outcome stays
            applied = version.with_outcome(command.outcome)
            if not uow.versions.set_outcome(version.id, applied.status):
                return OutcomeView(OutcomeResult.REFUSED)  # lost a race to another outcome
            now = self._clock.now()
            who = attribution_of(ctx)
            uow.audit.record(
                AuditFact(
                    tenant_id=ctx.tenant_id,
                    actor=who.actor,
                    action="documents.version_outcome_recorded",
                    resource_type="document",
                    resource_id=str(document.id),
                    outcome=AuditOutcome.SUCCESS,
                    request_id=who.request_id,
                    occurred_at=now,
                    attributes={
                        **who.attributes,
                        "version_id": str(version.id),
                        "status": command.outcome.value,
                    },
                )
            )
            uow.commit()
            return OutcomeView(OutcomeResult.APPLIED)
