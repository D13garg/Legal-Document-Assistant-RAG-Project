"""Read-only operator visibility for jobs that need attention (Step 18).

ADR-005 and ADR-023 tell operators that a dead `documents:upload_intent` job means an orphan needs
attention and a dead `documents:purge` job means a deleted document is not yet purged. Both live on
the `documents` queue, and `metrics()` groups only by queue and status, so the two were
indistinguishable. These tests pin a read-only summary by job type and an overdue-lease count.
They prove the SQL and its isolation properties on real PostgreSQL; they do not prove that anyone
watches the numbers: no exporter, alert or dashboard exists.
"""

from __future__ import annotations

from datetime import timedelta

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
) -> None:
    with TenantTransaction(engine, tenant(n)) as tx:
        PostgresJobQueue(tx, clock, ids).enqueue(spec)
        tx.commit()


def kill(engine: Engine, clock: FakeClock, queue: str, error_code: str, count: int = 1) -> None:
    store = JobLeaseStore(engine, clock)
    for _ in range(count):
        (job,) = store.claim(queue, 1, "w", LEASE)
        assert store.fail(job.id, "w", job.fencing_token, error_code)


def test_dead_jobs_are_grouped_by_type_so_purge_and_orphan_recovery_are_distinguishable(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="documents", job_type="documents:purge"))
    kill(app_engine, clock, "documents", "object_store_unavailable")
    enqueue(app_engine, clock, ids, 2, JobSpec(queue="documents", job_type="documents:purge"))
    kill(app_engine, clock, "documents", "object_store_unavailable")
    enqueue(
        app_engine, clock, ids, 1, JobSpec(queue="documents", job_type="documents:upload_intent")
    )
    kill(app_engine, clock, "documents", "no_handler")
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="documents", job_type="documents:purge"))
    clock.advance(timedelta(seconds=90))

    summary = JobMaintenance(app_engine, clock).dead_letter_summary()  # cross-tenant, read-only

    assert [(g.queue, g.job_type, g.last_error_code, g.count) for g in summary] == [
        ("documents", "documents:purge", "object_store_unavailable", 2),
        ("documents", "documents:upload_intent", "no_handler", 1),
    ]  # the still-queued purge job is not "dead"
    assert all(g.oldest_age_seconds == 90.0 for g in summary)


def test_the_summary_reads_only_and_exposes_no_payload_or_tenant(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(
        app_engine,
        clock,
        ids,
        1,
        JobSpec(queue="q", job_type="t", payload={"secret": "TOP-SECRET-clause"}),
    )
    kill(app_engine, clock, "q", "boom")
    with admin_engine.connect() as conn:
        before = conn.execute(text("SELECT count(*), max(updated_at) FROM platform.jobs")).one()
    summary = JobMaintenance(app_engine, clock).dead_letter_summary()
    with admin_engine.connect() as conn:
        after = conn.execute(text("SELECT count(*), max(updated_at) FROM platform.jobs")).one()
    assert before == after  # nothing written
    assert "TOP-SECRET" not in repr(summary)
    assert not any(hasattr(g, "tenant_id") or hasattr(g, "payload") for g in summary)


def test_an_empty_queue_has_nothing_that_needs_attention(
    app_engine: Engine, clock: FakeClock
) -> None:
    maintenance = JobMaintenance(app_engine, clock)
    assert maintenance.dead_letter_summary() == []
    assert maintenance.overdue_lease_count() == 0


def test_a_lease_past_its_expiry_is_counted_until_it_is_reclaimed(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    store.claim("q", 1, "dead-worker", LEASE)
    assert maintenance.overdue_lease_count() == 0  # lease still valid: not overdue
    clock.advance(LEASE + timedelta(seconds=1))
    assert maintenance.overdue_lease_count() == 1  # the worker is gone and nothing reclaimed yet
    assert maintenance.reclaim_expired() == 1
    assert maintenance.overdue_lease_count() == 0
