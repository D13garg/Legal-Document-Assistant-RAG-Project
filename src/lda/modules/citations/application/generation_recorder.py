"""`GenerationRecorder`: write the insert-only `GenerationRecord` of one generation (ADR-016).

Not a use case (it has no `execute` and sits outside `use_cases/`); the caller, a later step's use
case, owns authorization and audit declarations. It checks, before writing, that the evidence
belongs to the caller's tenant and principal and that every citation is one the evidence issued:
a citation built by hand, or taken from another generation, is refused.
"""

from __future__ import annotations

from lda.modules.citations.application.dto import RecordedGeneration, RecordGenerationCommand
from lda.modules.citations.application.ports import GenerationRecordRepository
from lda.modules.citations.domain.codes import CITATION_NOT_IN_EVIDENCE, EVIDENCE_FOREIGN
from lda.modules.citations.domain.record import GenerationRecord
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import AuthorizationError, ValidationError
from lda.shared.ports.clock import Clock, IdGenerator


class GenerationRecorder:
    def __init__(self, records: GenerationRecordRepository, clock: Clock, ids: IdGenerator) -> None:
        self._records = records
        self._clock = clock
        self._ids = ids

    def record(self, ctx: TenantContext, command: RecordGenerationCommand) -> RecordedGeneration:
        evidence = command.evidence
        if (evidence.tenant_id, evidence.principal_id) != (ctx.tenant_id, ctx.principal_id):
            raise AuthorizationError(EVIDENCE_FOREIGN)
        if not isinstance(command.citations, tuple) or not all(
            evidence.contains(c) for c in command.citations
        ):
            raise ValidationError(CITATION_NOT_IN_EVIDENCE)
        record = GenerationRecord(
            record_id=self._ids.new_uuid(),
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            request_id=command.request_id,
            created_at=self._clock.now(),
            config_id=command.config_id,
            outcome=command.outcome,
            template_id=command.template_id,
            template_version=command.template_version,
            model_id=command.model_id,
            request_sha256=command.request_sha256,
            evidence_fingerprint=evidence.fingerprint(),
            citations=command.citations,
        )
        self._records.add(record)
        return RecordedGeneration(record.record_id)
