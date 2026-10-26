from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from lda.platform.database.jobs.tables import jobs
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.ids import JobId
from lda.shared.ports.clock import Clock, IdGenerator
from lda.shared.ports.jobs import JobSpec


class PostgresJobQueue:
    """JobQueue bound to one tenant transaction: the row commits or rolls back with the caller."""

    def __init__(self, tx: TenantTransaction, clock: Clock, ids: IdGenerator) -> None:
        self._tx = tx
        self._clock = clock
        self._ids = ids

    def enqueue(self, spec: JobSpec) -> JobId | None:
        now = self._clock.now()
        statement = (
            insert(jobs)
            .values(
                id=self._ids.new_uuid(),
                tenant_id=self._tx.tenant_id.value,
                queue=spec.queue,
                job_type=spec.job_type,
                payload=dict(spec.payload),
                status="queued",
                attempts=0,
                max_attempts=spec.max_attempts,
                run_at=spec.run_at or now,
                unique_key=spec.unique_key,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "unique_key"],
                index_where=jobs.c.unique_key.is_not(None),
            )
            .returning(jobs.c.id)
        )
        row = self._tx.session.execute(statement).first()
        return JobId(row.id) if row else None
