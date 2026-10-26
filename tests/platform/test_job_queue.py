from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.ports.jobs import JobSpec
from platform_support import tenant

LEASE = timedelta(seconds=30)


def enqueue(
    engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, n: int, spec: JobSpec
) -> object:
    with TenantTransaction(engine, tenant(n)) as tx:
        job_id = PostgresJobQueue(tx, clock, ids).enqueue(spec)
        tx.commit()
    return job_id


def statuses(admin: Engine) -> dict[str, str]:
    with admin.connect() as conn:
        return {
            r.job_type: r.status
            for r in conn.execute(text("SELECT job_type, status FROM platform.jobs"))
        }


def test_enqueue_is_transactional(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    with TenantTransaction(app_engine, tenant(1)) as tx:
        PostgresJobQueue(tx, clock, ids).enqueue(JobSpec(queue="q", job_type="rolled_back"))
        tx.rollback()
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="kept"))
    assert statuses(admin_engine) == {"kept": "queued"}


def test_unique_key_makes_enqueue_idempotent_per_tenant(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    spec = JobSpec(queue="q", job_type="t", unique_key="k")
    assert enqueue(app_engine, clock, ids, 1, spec) is not None
    assert enqueue(app_engine, clock, ids, 1, spec) is None
    assert enqueue(app_engine, clock, ids, 2, spec) is not None  # same key, other tenant


def test_claim_complete_and_tenant_is_authoritative(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 7, JobSpec(queue="q", job_type="t"))
    store = JobLeaseStore(app_engine, clock)
    (job,) = store.claim("q", 5, "w1", LEASE)
    assert job.tenant_id == tenant(7) and job.attempt == 1
    assert store.claim("q", 5, "w2", LEASE) == []  # already leased
    assert not store.complete(job.id, "w2", job.fencing_token)  # only the lease owner may complete
    assert store.complete(job.id, "w1", job.fencing_token)
    assert statuses(admin_engine) == {"t": "succeeded"}


def test_run_at_is_respected(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    later = clock.now() + timedelta(minutes=5)
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", run_at=later))
    store = JobLeaseStore(app_engine, clock)
    assert store.claim("q", 5, "w", LEASE) == []
    clock.advance(timedelta(minutes=6))
    assert len(store.claim("q", 5, "w", LEASE)) == 1


def test_retry_then_dead_letter_then_replay(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", max_attempts=2))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    (job,) = store.claim("q", 1, "w", LEASE)
    assert store.retry(
        job.id, "w", job.fencing_token, timedelta(seconds=10), "boom", consumes_attempt=True
    )
    assert store.claim("q", 1, "w", LEASE) == []  # backoff not elapsed
    clock.advance(timedelta(seconds=11))
    (job,) = store.claim("q", 1, "w", LEASE)
    assert job.attempt == 2
    store.retry(job.id, "w", job.fencing_token, timedelta(seconds=1), "boom", consumes_attempt=True)
    assert statuses(admin_engine) == {"t": "dead"}  # attempts exhausted -> DLQ
    assert maintenance.replay(job.id)
    assert statuses(admin_engine) == {"t": "queued"}


def test_provider_outage_backoff_does_not_burn_attempts(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", max_attempts=1))
    store = JobLeaseStore(app_engine, clock)
    for _ in range(3):
        (job,) = store.claim("q", 1, "w", LEASE)
        store.retry(
            job.id,
            "w",
            job.fencing_token,
            timedelta(seconds=1),
            "provider_unavailable",
            consumes_attempt=False,
        )
        clock.advance(timedelta(seconds=2))
    assert statuses(admin_engine) == {"t": "queued"}


def test_expired_lease_is_reclaimed_and_stale_owner_cannot_finish(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    (job,) = store.claim("q", 1, "dead-worker", LEASE)
    assert maintenance.reclaim_expired() == 0  # lease still valid
    clock.advance(LEASE + timedelta(seconds=1))
    assert maintenance.reclaim_expired() == 1
    (again,) = store.claim("q", 1, "w2", LEASE)
    assert again.id == job.id and again.attempt == 2
    assert not store.complete(job.id, "dead-worker", job.fencing_token)


def test_claim_respects_per_queue_limit_and_isolation(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    for i in range(3):
        enqueue(app_engine, clock, ids, 1, JobSpec(queue="ocr", job_type=f"o{i}"))
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="embed", job_type="e"))
    store = JobLeaseStore(app_engine, clock)
    assert len(store.claim("ocr", 2, "w", LEASE)) == 2
    assert len(store.claim("embed", 2, "w", LEASE)) == 1


def test_metrics_report_depth_and_oldest_age(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    clock.advance(timedelta(seconds=90))
    (metric,) = JobMaintenance(app_engine, clock).metrics()
    assert (metric.queue, metric.status, metric.count) == ("q", "queued", 1)
    assert metric.oldest_queued_age_seconds == pytest.approx(90)
