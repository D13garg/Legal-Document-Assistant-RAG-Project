"""Documents' reactions to events (ADR-010 §2, §7, §10): idempotent, tenant-bound, ids only."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import (
    OutcomeResult,
    OutcomeView,
    RecordOutcomeCommand,
)
from lda.modules.documents.application.event_handlers import (
    CancelPipelineOnDocumentDeleted,
    ClearPipelineRecordsOnPurge,
    RecordFailedOnFailed,
    RecordQuarantinedOnQuarantined,
    RecordReadyOnCompleted,
    StartPipelineOnVersionAdded,
)
from lda.modules.documents.domain.document import VersionStatus
from lda.modules.documents.domain.events import (
    DocumentDeleted,
    DocumentPurged,
    DocumentVersionAdded,
)
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.tests.support import START, TENANT_A, TENANT_B
from lda.modules.ingestion.api import (
    DocumentRef,
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
    IngestionRunId,
    VersionRef,
)
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import EventId, TenantId
from lda.shared.ports.jobs import JobSpec

D, V, RUN = UUID(int=0xD0C), UUID(int=0x7E5), UUID(int=0x2A)


@dataclass
class FakeUnitOfWork:
    """The generic unit of work the platform hands an event handler: only `jobs` is used."""

    enqueued: list[JobSpec] = field(default_factory=list)

    @property
    def jobs(self) -> FakeUnitOfWork:
        return self

    def enqueue(self, spec: JobSpec) -> None:
        self.enqueued.append(spec)


class Recorder:
    def __init__(self, result: OutcomeResult = OutcomeResult.APPLIED) -> None:
        self.result = result
        self.calls: list[tuple[SystemContext, object]] = []

    def execute(self, ctx: SystemContext, command: object) -> object:
        self.calls.append((ctx, command))
        return OutcomeView(self.result) if isinstance(command, RecordOutcomeCommand) else 1


def completed(tenant: TenantId = TENANT_A) -> IngestionCompleted:
    return IngestionCompleted(
        event_id=EventId(UUID(int=1)),
        tenant_id=tenant,
        occurred_at=START,
        document=DocumentRef(D),
        version=VersionRef(V),
        run_id=IngestionRunId(RUN),
        stage_count=2,
    )


# ---- start -------------------------------------------------------------------------------------


def test_a_new_version_enqueues_exactly_one_ingestion_job_with_ids_only() -> None:
    uow = FakeUnitOfWork()
    event = DocumentVersionAdded(
        event_id=EventId(UUID(int=1)),
        tenant_id=TENANT_A,
        occurred_at=START,
        document_id=DocumentId(D),
        version_id=DocumentVersionId(V),
        version_number=1,
    )
    StartPipelineOnVersionAdded().handle(event, uow)  # type: ignore[arg-type]
    (spec,) = uow.enqueued
    assert (spec.queue, spec.job_type) == ("ingestion", "ingestion:run")
    assert dict(spec.payload) == {"document_id": str(D), "version_id": str(V), "generation": 1}
    assert spec.unique_key == f"ingestion:run:{V}:1"
    assert "pdf" not in repr(spec.payload) and "tenant" not in repr(spec.payload)


def failed(tenant: TenantId = TENANT_A) -> IngestionFailed:
    return IngestionFailed(
        event_id=EventId(UUID(int=3)),
        tenant_id=tenant,
        occurred_at=START,
        document=DocumentRef(D),
        version=VersionRef(V),
        run_id=IngestionRunId(RUN),
        error_code="bad",
    )


def quarantined(tenant: TenantId = TENANT_A) -> IngestionQuarantined:
    return IngestionQuarantined(
        event_id=EventId(UUID(int=4)),
        tenant_id=tenant,
        occurred_at=START,
        document=DocumentRef(D),
        version=VersionRef(V),
        run_id=IngestionRunId(RUN),
        error_code="malware_detected",
    )


def purged_event() -> DocumentPurged:
    return DocumentPurged(
        event_id=EventId(UUID(int=7)),
        tenant_id=TENANT_A,
        occurred_at=START,
        document_id=DocumentId(D),
        versions_purged=1,
    )


@pytest.mark.parametrize(
    ("handler", "wrong_event"),
    [
        (StartPipelineOnVersionAdded(), purged_event()),
        (RecordReadyOnCompleted(None, None, None), failed()),  # type: ignore[arg-type]
        (RecordFailedOnFailed(None, None, None), completed()),  # type: ignore[arg-type]
        (RecordQuarantinedOnQuarantined(None, None, None), completed()),  # type: ignore[arg-type]
        (CancelPipelineOnDocumentDeleted(None), purged_event()),  # type: ignore[arg-type]
        (ClearPipelineRecordsOnPurge(None), completed()),  # type: ignore[arg-type]
    ],
)
def test_every_handler_refuses_an_event_of_another_type(
    handler: object, wrong_event: object
) -> None:
    with pytest.raises(ValidationError, match="event_type_unexpected"):
        handler.handle(wrong_event, FakeUnitOfWork())  # type: ignore[attr-defined]


def test_handler_names_are_stable_and_unique() -> None:
    names = [
        StartPipelineOnVersionAdded.name,
        RecordReadyOnCompleted.name,
        RecordFailedOnFailed.name,
        RecordQuarantinedOnQuarantined.name,
        CancelPipelineOnDocumentDeleted.name,
        ClearPipelineRecordsOnPurge.name,
    ]
    assert names == [
        "documents.start_pipeline",
        "documents.record_ready",
        "documents.record_failed",
        "documents.record_quarantined",
        "documents.cancel_pipeline",
        "documents.clear_pipeline_records",
    ]


# ---- outcomes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler_type", "event", "expected"),
    [
        (RecordReadyOnCompleted, completed(), VersionStatus.READY),
        (RecordFailedOnFailed, failed(), VersionStatus.FAILED),
        (RecordQuarantinedOnQuarantined, quarantined(), VersionStatus.QUARANTINED),
    ],
)
def test_each_outcome_event_records_its_own_status_then_acknowledges(
    handler_type: type, event: object, expected: VersionStatus
) -> None:
    record, ack, purge = Recorder(), Recorder(), Recorder()
    handler_type(record, ack, purge).handle(event, FakeUnitOfWork())
    ((rctx, rcmd),) = record.calls
    assert (rctx.tenant_id, rcmd) == (
        TENANT_A,
        RecordOutcomeCommand(DocumentId(D), DocumentVersionId(V), expected),
    )
    ((actx, acmd),) = ack.calls
    assert actx.tenant_id == TENANT_A and acmd.run_id == IngestionRunId(RUN)  # type: ignore[attr-defined]
    assert purge.calls == []


def test_the_handlers_context_is_the_events_tenant_never_anything_else() -> None:
    record, ack, purge = Recorder(), Recorder(), Recorder()
    RecordReadyOnCompleted(record, ack, purge).handle(completed(TENANT_B), FakeUnitOfWork())  # type: ignore[arg-type]
    assert record.calls[0][0].tenant_id == TENANT_B and ack.calls[0][0].tenant_id == TENANT_B


@pytest.mark.parametrize(
    ("result", "purges"),
    [
        (OutcomeResult.APPLIED, False),
        (OutcomeResult.ALREADY, False),
        (OutcomeResult.REFUSED, False),
        (OutcomeResult.DELETED, False),  # the purge handler will clean up after the tombstone
        (OutcomeResult.MISSING, True),  # the version is gone: ingestion's records go too
    ],
)
def test_ingestion_records_are_purged_only_when_the_version_no_longer_exists(
    result: OutcomeResult, purges: bool
) -> None:
    record, ack, purge = Recorder(result), Recorder(), Recorder()
    RecordReadyOnCompleted(record, ack, purge).handle(completed(), FakeUnitOfWork())  # type: ignore[arg-type]
    assert len(ack.calls) == 1  # always acknowledged: refusing is also an answer
    assert len(purge.calls) == (1 if purges else 0)
    if purges:
        assert purge.calls[0][1].document == DocumentRef(D)  # type: ignore[attr-defined]


# ---- cleanup -----------------------------------------------------------------------------------


def test_deletion_cancels_and_purge_clears_ingestion_state_for_that_document() -> None:
    cancel, clear = Recorder(), Recorder()
    deleted = DocumentDeleted(
        event_id=EventId(UUID(int=5)),
        tenant_id=TENANT_A,
        occurred_at=START,
        document_id=DocumentId(D),
    )
    CancelPipelineOnDocumentDeleted(cancel).handle(deleted, FakeUnitOfWork())  # type: ignore[arg-type]
    ((ctx, command),) = cancel.calls
    assert ctx.tenant_id == TENANT_A and command.document == DocumentRef(D)  # type: ignore[attr-defined]
    purged = DocumentPurged(
        event_id=EventId(UUID(int=6)),
        tenant_id=TENANT_B,
        occurred_at=START,
        document_id=DocumentId(D),
        versions_purged=1,
    )
    ClearPipelineRecordsOnPurge(clear).handle(purged, FakeUnitOfWork())  # type: ignore[arg-type]
    ((ctx2, command2),) = clear.calls
    assert ctx2.tenant_id == TENANT_B and command2.document == DocumentRef(D)  # type: ignore[attr-defined]
