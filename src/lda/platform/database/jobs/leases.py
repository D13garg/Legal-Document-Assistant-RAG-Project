from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Engine, text

from lda.platform.database.transaction import SystemQueueTransaction
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.clock import Clock
from lda.shared.ports.jobs import ClaimedJob

_CLAIM = text(
    """
    WITH picked AS (
        SELECT id FROM platform.jobs
        WHERE status = 'queued' AND queue = :queue AND run_at <= :now
        ORDER BY run_at, created_at
        FOR UPDATE SKIP LOCKED
        LIMIT :limit
    )
    UPDATE platform.jobs j
    SET status = 'running', attempts = j.attempts + 1, lease_owner = :worker,
        lease_expires_at = :expires, updated_at = :now, fencing_token = j.fencing_token + 1
    FROM picked WHERE j.id = picked.id
    RETURNING j.id, j.tenant_id, j.queue, j.job_type, j.payload, j.attempts,
        j.max_attempts, j.fencing_token
    """
)
# Per-tenant cap (ADR-011): claims on one queue are serialized by the advisory lock taken first,
# so the running counts cannot be outdated by a concurrent claimer.
_CLAIM_LOCK = text("SELECT pg_advisory_xact_lock(hashtextextended(:queue, 0))")
_CLAIM_CAPPED = text(
    """
    WITH running AS (
        SELECT tenant_id, count(*) AS n FROM platform.jobs
        WHERE status = 'running' AND queue = :queue GROUP BY tenant_id
    ),
    ranked AS (
        SELECT j.id, j.run_at, j.created_at,
               row_number() OVER (
                   PARTITION BY j.tenant_id ORDER BY j.run_at, j.created_at
               ) AS rn,
               COALESCE(r.n, 0) AS busy
        FROM platform.jobs j LEFT JOIN running r ON r.tenant_id = j.tenant_id
        WHERE j.status = 'queued' AND j.queue = :queue AND j.run_at <= :now
    ),
    picked AS (
        SELECT id FROM ranked WHERE rn + busy <= :cap ORDER BY run_at, created_at LIMIT :limit
    )
    UPDATE platform.jobs j
    SET status = 'running', attempts = j.attempts + 1, lease_owner = :worker,
        lease_expires_at = :expires, updated_at = :now, fencing_token = j.fencing_token + 1
    FROM picked WHERE j.id = picked.id
    RETURNING j.id, j.tenant_id, j.queue, j.job_type, j.payload, j.attempts,
        j.max_attempts, j.fencing_token
    """
)
_HEARTBEAT = text(
    "UPDATE platform.jobs SET lease_expires_at = :expires, updated_at = :now "
    "WHERE id = :id AND lease_owner = :worker AND fencing_token = :token AND status = 'running'"
)
_COMPLETE = text(
    "UPDATE platform.jobs SET status = 'succeeded', lease_owner = NULL, lease_expires_at = NULL, "
    "updated_at = :now, finished_at = :now "
    "WHERE id = :id AND lease_owner = :worker AND fencing_token = :token AND status = 'running'"
)
_RETRY = text(
    """
    UPDATE platform.jobs SET
        status = CASE WHEN :consumes AND attempts >= max_attempts THEN 'dead' ELSE 'queued' END,
        finished_at = CASE WHEN :consumes AND attempts >= max_attempts THEN :now END,
        attempts = CASE WHEN :consumes THEN attempts ELSE GREATEST(attempts - 1, 0) END,
        run_at = :run_at, lease_owner = NULL, lease_expires_at = NULL,
        last_error_code = :code, updated_at = :now
    WHERE id = :id AND lease_owner = :worker AND fencing_token = :token AND status = 'running'
    """
)
_FAIL = text(
    "UPDATE platform.jobs SET status = 'dead', lease_owner = NULL, lease_expires_at = NULL, "
    "last_error_code = :code, updated_at = :now, finished_at = :now "
    "WHERE id = :id AND lease_owner = :worker AND fencing_token = :token AND status = 'running'"
)


class JobLeaseStore:
    """Claim/heartbeat/complete/retry/fail. Every mutation requires the caller's lease: the worker
    id AND the fencing token of its claim, so a stale worker fails closed (ADR-012 amendment)."""

    def __init__(self, engine: Engine, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def claim(
        self,
        queue: str,
        limit: int,
        worker_id: str,
        lease: timedelta,
        *,
        max_per_tenant: int | None = None,
    ) -> list[ClaimedJob]:
        now = self._clock.now()
        params: dict[str, object] = {
            "queue": queue,
            "limit": limit,
            "worker": worker_id,
            "now": now,
            "expires": now + lease,
        }
        with SystemQueueTransaction(self._engine) as tx:
            if max_per_tenant is None:
                rows = tx.session.execute(_CLAIM, params).all()
            else:
                tx.session.execute(_CLAIM_LOCK, {"queue": queue})
                rows = tx.session.execute(_CLAIM_CAPPED, {**params, "cap": max_per_tenant}).all()
            tx.commit()
        return [
            ClaimedJob(
                id=JobId(r.id),
                tenant_id=TenantId(r.tenant_id),
                queue=r.queue,
                job_type=r.job_type,
                payload=r.payload,
                attempt=r.attempts,
                max_attempts=r.max_attempts,
                fencing_token=r.fencing_token,
            )
            for r in rows
        ]

    def heartbeat(
        self, job_id: JobId, worker_id: str, fencing_token: int, lease: timedelta
    ) -> bool:
        now = self._clock.now()
        return self._mutate(
            _HEARTBEAT, job_id, worker_id, fencing_token, now=now, expires=now + lease
        )

    def complete(self, job_id: JobId, worker_id: str, fencing_token: int) -> bool:
        return self._mutate(_COMPLETE, job_id, worker_id, fencing_token, now=self._clock.now())

    def retry(
        self,
        job_id: JobId,
        worker_id: str,
        fencing_token: int,
        delay: timedelta,
        error_code: str,
        *,
        consumes_attempt: bool,
    ) -> bool:
        now = self._clock.now()
        return self._mutate(
            _RETRY,
            job_id,
            worker_id,
            fencing_token,
            now=now,
            run_at=now + delay,
            code=error_code,
            consumes=consumes_attempt,
        )

    def fail(self, job_id: JobId, worker_id: str, fencing_token: int, error_code: str) -> bool:
        return self._mutate(
            _FAIL, job_id, worker_id, fencing_token, now=self._clock.now(), code=error_code
        )

    def _mutate(
        self,
        statement: object,
        job_id: JobId,
        worker_id: str,
        fencing_token: int,
        **values: object,
    ) -> bool:
        params = {"id": job_id.value, "worker": worker_id, "token": fencing_token, **values}
        with SystemQueueTransaction(self._engine) as tx:
            count = tx.execute_count(statement, params)
            tx.commit()
        return bool(count)
