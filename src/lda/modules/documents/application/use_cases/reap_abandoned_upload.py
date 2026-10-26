from __future__ import annotations

from datetime import timedelta

from lda.modules.documents.application.dto.documents import ReapUploadCommand, ReapView
from lda.modules.documents.application.policy import UploadPolicy
from lda.modules.documents.application.ports import DocumentsUnitOfWorkFactory
from lda.modules.documents.domain.resource import original_path
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.declarations import not_audited, public
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.ports.clock import Clock
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey


@public(reason="Runs only as the delayed system job enqueued with every upload intent.")
@not_audited(reason="Housekeeping of an unfinished upload, not a business mutation (ADR-019 D9).")
class ReapAbandonedUpload:
    """Removes the object of an upload whose rows never committed (ADR-005 step 4).

    The intent row is locked for the whole decision, so a slow upload's own transaction either
    commits first (a version exists, the intent is stale) or finds the intent gone and fails.
    """

    def __init__(
        self,
        units_of_work: DocumentsUnitOfWorkFactory,
        store: ObjectStore,
        clock: Clock,
        policy: UploadPolicy,
    ) -> None:
        self._units_of_work = units_of_work
        self._store = store
        self._clock = clock
        self._policy = policy

    def execute(self, ctx: ExecutionContext, command: ReapUploadCommand) -> ReapView:
        with self._units_of_work.begin(ctx) as uow:
            intent = uow.intents.get(command.version_id, for_update=True)
            if intent is None:
                return ReapView("already_clean")
            due = intent.created_at + timedelta(seconds=self._policy.grace_seconds)
            now = self._clock.now()
            if now < due:
                return ReapView(
                    "not_due", retry_after_seconds=max(1, int((due - now).total_seconds()))
                )
            if uow.versions.get(command.version_id) is not None:
                uow.intents.remove(command.version_id)
                uow.commit()
                return ReapView("version_exists")
            key = TenantScopedKey(
                ctx.tenant_id, original_path(command.document_id, command.version_id)
            )
            self._store.delete(key)
            if self._store.exists(key):
                raise InfrastructureError("orphan_removal_unverified")
            uow.intents.remove(command.version_id)
            uow.commit()
            return ReapView("orphan_removed")
