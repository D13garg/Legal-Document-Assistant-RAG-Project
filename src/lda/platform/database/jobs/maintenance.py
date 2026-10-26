from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, text

from lda.platform.database.transaction import SystemQueueTransaction
from lda.shared.kernel.ids import JobId
from lda.shared.ports.clock import Clock

_RECLAIM = text(
    """
    UPDATE platform.jobs SET
        status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
        finished_at = CASE WHEN attempts >= max_attempts THEN :now END,
        run_at = :now, lease_owner = NULL, lease_expires_at = NULL,
        fencing_token = fencing_token + 1,
        last_error_code = 'lease_expired', updated_at = :now
    WHERE status = 'running' AND lease_expires_at < :now
    """
)
_REPLAY = text(
    "UPDATE platform.jobs SET status = 'queued', attempts = 0, run_at = :now, finished_at = NULL, "
    "last_error_code = NULL, updated_at = :now WHERE id = :id AND status = 'dead'"
)
_METRICS = text(
    """
    SELECT queue, status, count(*) AS n,
           EXTRACT(EPOCH FROM (CAST(:now AS timestamptz)
               - min(run_at) FILTER (WHERE status = 'queued'))) AS oldest_age
    FROM platform.jobs GROUP BY queue, status ORDER BY queue, status
    """
)
_DEAD_LETTERS = text(
    """
    SELECT queue, job_type, last_error_code, count(*) AS n,
           EXTRACT(EPOCH FROM (CAST(:now AS timestamptz) - min(finished_at))) AS oldest_age
    FROM platform.jobs WHERE status = 'dead'
    GROUP BY queue, job_type, last_error_code ORDER BY queue, job_type, last_error_code
    """
)
_OVERDUE_LEASES = text(
    "SELECT count(*) FROM platform.jobs WHERE status = 'running' AND lease_expires_at < :now"
)


@dataclass(frozen=True, slots=True)
class DeadLetterGroup:
    """Dead jobs of one type and error code. Identifiers and counts only: no tenant, no payload."""

    queue: str
    job_type: str
    last_error_code: str | None
    count: int
    oldest_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class QueueMetric:
    queue: str
    status: str
    count: int
    oldest_queued_age_seconds: float | None


class JobMaintenance:
    """Recovery, replay and observability for the queue (cross-tenant, identifiers only)."""

    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def reclaim_expired(self) -> int:
        """Return jobs of dead workers to the queue (or the DLQ when attempts are exhausted)."""
        with SystemQueueTransaction(self._engine) as tx:
            count = tx.execute_count(_RECLAIM, {"now": self._clock.now()})
            tx.commit()
        return count

    def replay(self, job_id: JobId) -> bool:
        """Re-queue a dead-lettered job. Callers audit this action."""
        with SystemQueueTransaction(self._engine) as tx:
            count = tx.execute_count(_REPLAY, {"id": job_id.value, "now": self._clock.now()})
            tx.commit()
        return bool(count)

    def metrics(self) -> list[QueueMetric]:
        with SystemQueueTransaction(self._engine) as tx:
            rows = tx.session.execute(_METRICS, {"now": self._clock.now()}).all()
            tx.rollback()
        return [
            QueueMetric(
                r.queue, r.status, r.n, float(r.oldest_age) if r.oldest_age is not None else None
            )
            for r in rows
        ]

    def dead_letter_summary(self) -> list[DeadLetterGroup]:
        """Read-only: dead jobs grouped by queue, type and error code (Step 18).

        Lets an operator tell a dead `documents:purge` job (a deleted document is not yet purged,
        ADR-023) from a dead `documents:upload_intent` job (an orphan needs attention, ADR-005),
        which `metrics()` cannot because both share a queue. Nothing here alerts anyone.
        """
        with SystemQueueTransaction(self._engine) as tx:
            rows = tx.session.execute(_DEAD_LETTERS, {"now": self._clock.now()}).all()
            tx.rollback()
        return [
            DeadLetterGroup(
                r.queue,
                r.job_type,
                r.last_error_code,
                r.n,
                float(r.oldest_age) if r.oldest_age is not None else None,
            )
            for r in rows
        ]

    def overdue_lease_count(self) -> int:
        """Read-only: running jobs whose lease expired and that `reclaim_expired` has not yet
        returned to the queue. A persistently non-zero value means no worker loop is reclaiming."""
        with SystemQueueTransaction(self._engine) as tx:
            count = tx.session.execute(_OVERDUE_LEASES, {"now": self._clock.now()}).scalar_one()
            tx.rollback()
        return int(count)
