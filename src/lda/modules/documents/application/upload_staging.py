"""Steps (a) and (b) of the upload protocol: a durable intent, then the object (ADR-005).

Step (c), the rows, is each use case's own transaction so that it also records the audit fact
(ADR-019). `discard` is the immediate compensation; the delayed intent job is the guarantee.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import timedelta

from lda.modules.documents.application.jobs import DOCUMENTS_QUEUE, UPLOAD_INTENT_JOB
from lda.modules.documents.application.policy import UploadPolicy
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.evidence import UploadIntent
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.domain.resource import original_path
from lda.shared.kernel.context import ExecutionContext
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.jobs import JobSpec
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey


@dataclass(frozen=True, slots=True)
class StagedOriginal:
    version_id: DocumentVersionId
    key: TenantScopedKey
    media_type: str
    size_bytes: int


class UploadStaging:
    def __init__(
        self,
        units_of_work: DocumentsUnitOfWorkFactory,
        store: ObjectStore,
        clock: Clock,
        ids: IdGenerator,
        policy: UploadPolicy,
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock
        self._ids = ids
        self._policy = policy

    def stage(
        self, ctx: ExecutionContext, document_id: DocumentId, media_type: str, body: bytes
    ) -> StagedOriginal:
        self._policy.check(media_type, len(body))
        version_id = DocumentVersionId(self._ids.new_uuid())
        key = TenantScopedKey(ctx.tenant_id, original_path(document_id, version_id))
        now = self._clock.now()
        with self._units_of_work.begin(ctx) as uow:
            uow.intents.add(UploadIntent(ctx.tenant_id, version_id, document_id, now))
            uow.jobs.enqueue(
                JobSpec(
                    queue=DOCUMENTS_QUEUE,
                    job_type=UPLOAD_INTENT_JOB,
                    payload={"document_id": str(document_id), "version_id": str(version_id)},
                    unique_key=f"{UPLOAD_INTENT_JOB}:{version_id}",
                    run_at=now + timedelta(seconds=self._policy.grace_seconds),
                )
            )
            uow.commit()
        self._store.put(key, body, content_type=media_type, overwrite=False)
        return StagedOriginal(version_id, key, media_type, len(body))

    def discard(self, staged: StagedOriginal) -> None:
        """Best effort only: the intent job removes whatever this cannot."""
        with contextlib.suppress(Exception):
            self._store.delete(staged.key)
