"""The one place that turns a finished run into its outcome event (ADR-010 §7)."""

from __future__ import annotations

from datetime import datetime

from lda.modules.ingestion.domain.errors import InvalidTransition
from lda.modules.ingestion.domain.events import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.modules.ingestion.domain.run import IngestionRun, RunStatus
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import EventId

OUTCOME_FALLBACK_CODE = "ingestion_failed"


def outcome_event(run: IngestionRun, event_id: EventId, now: datetime) -> DomainEvent:
    """`completed` -> IngestionCompleted, `failed` -> IngestionFailed, `quarantined` ->
    IngestionQuarantined. Any other status has no outcome to publish."""
    code = run.error_code or OUTCOME_FALLBACK_CODE
    if run.status is RunStatus.COMPLETED:
        return IngestionCompleted(
            event_id=event_id,
            tenant_id=run.tenant_id,
            occurred_at=now,
            document=run.document,
            version=run.version,
            run_id=run.id,
            stage_count=len(run.stages),
        )
    if run.status is RunStatus.FAILED:
        return IngestionFailed(
            event_id=event_id,
            tenant_id=run.tenant_id,
            occurred_at=now,
            document=run.document,
            version=run.version,
            run_id=run.id,
            error_code=code,
        )
    if run.status is RunStatus.QUARANTINED:
        return IngestionQuarantined(
            event_id=event_id,
            tenant_id=run.tenant_id,
            occurred_at=now,
            document=run.document,
            version=run.version,
            run_id=run.id,
            error_code=code,
        )
    raise InvalidTransition
