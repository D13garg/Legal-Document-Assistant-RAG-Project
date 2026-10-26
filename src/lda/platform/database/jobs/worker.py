from __future__ import annotations

import logging
import random
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta

from lda.platform.configuration.settings import JobWorkerSettings
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import LdaError
from lda.shared.ports.clock import Clock
from lda.shared.ports.jobs import ClaimedJob, Failed, JobHandler, JobOutcome, RetryAfter, Succeeded

log = logging.getLogger("lda.jobs")


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_seconds: float
    cap_seconds: float
    jitter: Callable[[], float] = random.random

    def delay(self, attempt: int) -> timedelta:
        exponent = min(max(attempt - 1, 0), 62)  # bounded: huge attempt counts must not overflow
        ceiling = min(self.cap_seconds, self.base_seconds * (2**exponent))
        return timedelta(seconds=ceiling * (0.5 + 0.5 * self.jitter()))


class JobWorker:
    """Claims jobs per queue (bounded concurrency per queue) and runs idempotent handlers."""

    def __init__(
        self,
        store: JobLeaseStore,
        handlers: Mapping[str, JobHandler],
        settings: JobWorkerSettings,
        clock: Clock,
        worker_id: str,
        backoff: BackoffPolicy,
        maintenance: JobMaintenance | None = None,
    ) -> None:
        self._store = store
        self._handlers = handlers
        self._settings = settings
        self._clock = clock
        self._worker_id = worker_id
        self._backoff = backoff
        self._maintenance = maintenance
        self._last_reclaim: datetime | None = None

    def run_once(self) -> int:
        self._reclaim_when_due()
        lease = timedelta(seconds=self._settings.lease_seconds)
        claimed: list[ClaimedJob] = []
        for queue, concurrency in self._settings.queues.items():
            claimed.extend(
                self._store.claim(
                    queue,
                    concurrency,
                    self._worker_id,
                    lease,
                    max_per_tenant=self._settings.max_running_per_tenant,
                )
            )
        if claimed:
            with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
                list(pool.map(self._run_job, claimed))
        return len(claimed)

    def _reclaim_when_due(self) -> None:
        """ADR-011: the loop returns expired leases to the queue; a failure never stops it."""
        if self._maintenance is None:
            return
        now = self._clock.now()
        interval = timedelta(seconds=self._settings.reclaim_interval_seconds)
        if self._last_reclaim is not None and now - self._last_reclaim < interval:
            return
        self._last_reclaim = now
        try:
            count = self._maintenance.reclaim_expired()
        except Exception:
            log.warning("job_reclaim_failed")
            return
        if count:
            log.info("jobs_reclaimed", extra={"fields": {"count": count}})

    def run_until(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if self.run_once() == 0:
                stop.wait(self._settings.poll_interval_seconds)

    def _run_job(self, job: ClaimedJob) -> None:
        handler = self._handlers.get(job.job_type)
        if handler is None:
            self._apply(job, Failed(error_code="no_handler"))
            return
        ctx = SystemContext(tenant_id=job.tenant_id, purpose=f"job:{job.job_type}", job_id=job.id)
        beat_stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat, args=(job, beat_stop), daemon=True)
        beat.start()
        try:
            outcome = handler.handle(job, ctx)
        except Exception as exc:
            code = exc.code if isinstance(exc, LdaError) else "unhandled_exception"
            log.warning(
                "job_handler_raised",
                extra={"fields": {"job_type": job.job_type, "error_code": code}},
            )
            outcome = RetryAfter(delay=self._backoff.delay(job.attempt), error_code=code)
        finally:
            beat_stop.set()
            beat.join(timeout=5)
        self._apply(job, outcome)

    def _heartbeat(self, job: ClaimedJob, stop: threading.Event) -> None:
        lease = timedelta(seconds=self._settings.lease_seconds)
        while not stop.wait(self._settings.heartbeat_seconds):
            try:
                renewed = self._store.heartbeat(job.id, self._worker_id, job.fencing_token, lease)
            except Exception:
                # A transient database error must not end renewal: the lease would expire while
                # the handler is still running and the job would be executed twice concurrently.
                log.warning("job_heartbeat_failed", extra={"fields": {"job_type": job.job_type}})
                continue
            if not renewed:
                return

    def _apply(self, job: ClaimedJob, outcome: JobOutcome) -> None:
        token = job.fencing_token
        if isinstance(outcome, Succeeded):
            ok = self._store.complete(job.id, self._worker_id, token)
        elif isinstance(outcome, RetryAfter):
            ok = self._store.retry(
                job.id,
                self._worker_id,
                token,
                outcome.delay,
                outcome.error_code,
                consumes_attempt=outcome.consumes_attempt,
            )
        else:
            ok = self._store.fail(job.id, self._worker_id, token, outcome.error_code)
        if (
            not ok
        ):  # lease lost: another worker owns the job now; idempotent handlers make this safe
            log.warning("job_lease_lost", extra={"fields": {"job_type": job.job_type}})
