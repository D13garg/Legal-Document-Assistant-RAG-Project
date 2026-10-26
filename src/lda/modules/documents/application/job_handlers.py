"""Adapters from the job queue to the use cases (ADR-005, ADR-023). Failures propagate: the worker
retries with backoff and finally dead-letters, which is how a stuck cleanup becomes visible."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from lda.modules.documents.application.dto.documents import DocumentReference, ReapUploadCommand
from lda.modules.documents.application.use_cases.purge_document import PurgeDocument
from lda.modules.documents.application.use_cases.reap_abandoned_upload import ReapAbandonedUpload
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.context import SystemContext
from lda.shared.ports.jobs import ClaimedJob, Failed, JobOutcome, RetryAfter, Succeeded


def _uuid(job: ClaimedJob, field: str) -> UUID | None:
    try:
        return UUID(str(job.payload[field]))
    except (KeyError, ValueError):
        return None


class PurgeJobHandler:
    def __init__(self, purge: PurgeDocument) -> None:
        self._purge = purge

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        document_id = _uuid(job, "document_id")
        if document_id is None:
            return Failed(error_code="job_payload_invalid")
        self._purge.execute(ctx, DocumentReference(DocumentId(document_id)))
        return Succeeded()


class UploadIntentJobHandler:
    def __init__(self, reap: ReapAbandonedUpload) -> None:
        self._reap = reap

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        document_id, version_id = _uuid(job, "document_id"), _uuid(job, "version_id")
        if document_id is None or version_id is None:
            return Failed(error_code="job_payload_invalid")
        result = self._reap.execute(
            ctx, ReapUploadCommand(DocumentId(document_id), DocumentVersionId(version_id))
        )
        if result.outcome == "not_due":
            return RetryAfter(
                delay=timedelta(seconds=result.retry_after_seconds),
                error_code="upload_intent_not_due",
                consumes_attempt=False,
            )
        return Succeeded()
