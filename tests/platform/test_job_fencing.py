"""Fencing token, effect fence, per-tenant cap and the worker's reclaim tick (ADR-011, ADR-012)."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from lda.platform.configuration.settings import JobWorkerSettings
from lda.platform.database.jobs.fence import PostgresJobFence
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError
from lda.shared.ports.jobs import ClaimedJob, JobOutcome, JobSpec, Succeeded
from platform_support import enqueue_job, job_rows, tenant

LEASE = timedelta(seconds=30)


def spec(key: str | None = None, queue: str = "q") -> JobSpec:
    return JobSpec(queue=queue, job_type="t", unique_key=key)


def claim_one(store: JobLeaseStore, worker: str = "w") -> ClaimedJob:
    (job,) = store.claim("q", 1, worker, LEASE)
    return job


# ---- the token itself --------------------------------------------------------------------------


def test_every_claim_gets_a_higher_token_than_the_previous_claim_of_the_same_job(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    first = claim_one(store)
    store.retry(
        first.id, "w", first.fencing_token, timedelta(0), "x", consumes_attempt=False
    )  # attempt-neutral: attempts goes DOWN, the token must not
    second = claim_one(store)
    assert second.attempt == first.attempt
    assert second.fencing_token == first.fencing_token + 1
    assert first.fencing_token >= 1


def test_a_wrong_token_with_the_right_worker_cannot_touch_the_job(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    bad = job.fencing_token + 1
    assert not store.heartbeat(job.id, "w", bad, LEASE)
    assert not store.complete(job.id, "w", bad)
    assert not store.retry(job.id, "w", bad, timedelta(0), "x", consumes_attempt=True)
    assert not store.fail(job.id, "w", bad, "x")
    assert job_rows(admin_engine)[0].status == "running"
    assert store.complete(job.id, "w", job.fencing_token)


def test_a_stale_worker_that_restarted_with_the_same_worker_id_fails_closed(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    maintenance = JobMaintenance(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    stale = claim_one(store, "same-id")
    clock.advance(LEASE + timedelta(seconds=1))
    assert maintenance.reclaim_expired() == 1
    fresh = claim_one(store, "same-id")  # same worker id: only the token tells them apart
    assert fresh.fencing_token > stale.fencing_token
    assert not store.heartbeat(stale.id, "same-id", stale.fencing_token, LEASE)
    assert not store.complete(stale.id, "same-id", stale.fencing_token)
    assert not store.fail(stale.id, "same-id", stale.fencing_token, "x")
    assert not store.retry(
        stale.id, "same-id", stale.fencing_token, timedelta(0), "x", consumes_attempt=True
    )
    assert job_rows(admin_engine)[0].status == "running"
    assert store.complete(fresh.id, "same-id", fresh.fencing_token)


def test_reclaim_advances_the_token_even_before_anyone_claims_again(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    clock.advance(LEASE + timedelta(seconds=1))
    JobMaintenance(app_engine, clock).reclaim_expired()
    with admin_engine.connect() as conn:
        token = conn.execute(text("SELECT fencing_token FROM platform.jobs")).scalar_one()
    assert token == job.fencing_token + 1


# ---- the effect fence --------------------------------------------------------------------------


def verify(engine: Engine, clock: FakeClock, job: ClaimedJob, n: int = 1) -> None:
    with TenantTransaction(engine, tenant(n)) as tx:
        PostgresJobFence(tx, clock).verify(job.id, job.fencing_token)
        tx.commit()


def test_the_fence_accepts_the_current_lease_and_rejects_every_stale_one(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    verify(app_engine, clock, job)  # current: fine
    stale = ClaimedJob(**{**job.__dict__, "fencing_token": job.fencing_token - 1})
    with pytest.raises(ConflictError, match="job_lease_lost"):
        verify(app_engine, clock, stale)
    clock.advance(LEASE + timedelta(seconds=1))  # lease expired, not yet reclaimed
    with pytest.raises(ConflictError, match="job_lease_lost"):
        verify(app_engine, clock, job)


def test_the_fence_rejects_a_job_of_another_tenant_and_a_finished_job(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    with pytest.raises(ConflictError, match="job_lease_lost"):
        verify(app_engine, clock, job, n=2)  # tenant 2 cannot see tenant 1's job
    assert store.complete(job.id, "w", job.fencing_token)
    with pytest.raises(ConflictError, match="job_lease_lost"):
        verify(app_engine, clock, job)


def test_a_stale_worker_cannot_commit_effects_after_another_worker_took_over(
    app_engine: Engine,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
) -> None:
    with admin_engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS public.fence_effects (who text)"))
        conn.execute(text("GRANT INSERT ON public.fence_effects TO PUBLIC"))
        conn.execute(text("TRUNCATE public.fence_effects"))
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    stale = claim_one(store, "w1")
    clock.advance(LEASE + timedelta(seconds=1))
    JobMaintenance(app_engine, clock).reclaim_expired()
    fresh = claim_one(store, "w2")

    def effect(job: ClaimedJob, who: str) -> None:
        with TenantTransaction(app_engine, tenant(1)) as tx:
            PostgresJobFence(tx, clock).verify(job.id, job.fencing_token)
            tx.session.execute(text("INSERT INTO public.fence_effects VALUES (:w)"), {"w": who})
            tx.commit()

    with pytest.raises(ConflictError):
        effect(stale, "stale")  # rolled back: nothing committed
    effect(fresh, "fresh")
    with admin_engine.connect() as conn:
        rows = conn.execute(text("SELECT who FROM public.fence_effects")).scalars().all()
    assert rows == ["fresh"]


def test_a_verified_effect_blocks_reclaim_until_it_commits(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    reclaimed: list[int] = []
    tx = TenantTransaction(app_engine, tenant(1))
    with tx:
        PostgresJobFence(tx, clock).verify(job.id, job.fencing_token)  # share lock held
        clock.advance(LEASE - timedelta(seconds=1))  # still valid for the verify above
        worker = threading.Thread(
            target=lambda: reclaimed.append(_reclaim_after_expiry(app_engine, clock))
        )
        worker.start()
        time.sleep(0.6)
        assert reclaimed == []  # the reclaim waits for the effect transaction
        tx.commit()
    worker.join(timeout=10)
    assert reclaimed == [1]


def _reclaim_after_expiry(engine: Engine, clock: FakeClock) -> int:
    clock.advance(timedelta(seconds=5))  # now past the lease
    return JobMaintenance(engine, clock).reclaim_expired()


# ---- per-tenant claim cap ----------------------------------------------------------------------


def seed(
    engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, counts: dict[int, int]
) -> None:
    for n, count in counts.items():
        for i in range(count):
            enqueue_job(engine, clock, ids, n, spec(f"t{n}-{i}"))
            clock.advance(timedelta(milliseconds=1))


def by_tenant(jobs: list[ClaimedJob]) -> dict[object, int]:
    out: dict[object, int] = {}
    for j in jobs:
        out[j.tenant_id] = out.get(j.tenant_id, 0) + 1
    return out


def test_the_cap_limits_one_tenant_and_leaves_room_for_the_others(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    seed(app_engine, clock, ids, {1: 5, 2: 2})
    got = store.claim("q", 10, "w", LEASE, max_per_tenant=2)
    assert by_tenant(got) == {tenant(1): 2, tenant(2): 2}
    assert store.claim("q", 10, "w", LEASE, max_per_tenant=2) == []  # both tenants at the cap
    first = next(j for j in got if j.tenant_id == tenant(1))
    store.complete(first.id, "w", first.fencing_token)
    again = store.claim("q", 10, "w", LEASE, max_per_tenant=2)
    assert by_tenant(again) == {tenant(1): 1}  # a slot opened for tenant 1 only


def test_without_a_cap_nothing_changes(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seed(app_engine, clock, ids, {1: 5})
    store = JobLeaseStore(app_engine, clock)
    assert len(store.claim("q", 10, "w", LEASE)) == 5


def test_the_cap_is_global_per_queue_even_with_concurrent_claimers(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seed(app_engine, clock, ids, {1: 12})
    store = JobLeaseStore(app_engine, clock)
    claimed: list[ClaimedJob] = []
    lock = threading.Lock()

    def claimer(slot: int) -> None:
        got = store.claim("q", 12, f"w{slot}", LEASE, max_per_tenant=3)
        with lock:
            claimed.extend(got)

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(claimed) == 3
    assert len({j.id for j in claimed}) == 3


def test_the_cap_is_per_queue(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec("a", "q"))
    enqueue_job(app_engine, clock, ids, 1, spec("b", "other"))
    assert len(store.claim("q", 5, "w", LEASE, max_per_tenant=1)) == 1
    assert len(store.claim("other", 5, "w", LEASE, max_per_tenant=1)) == 1


# ---- the worker: reclaim tick and cap ----------------------------------------------------------


class CountingMaintenance(JobMaintenance):
    def __init__(self, engine: Engine, clock: FakeClock, *, fail: bool = False) -> None:
        super().__init__(engine, clock)
        self.calls = 0
        self.fail = fail

    def reclaim_expired(self) -> int:
        self.calls += 1
        if self.fail:
            raise RuntimeError("database unavailable")
        return super().reclaim_expired()


class Done:
    def __init__(self) -> None:
        self.seen: list[ClaimedJob] = []

    def handle(self, job: ClaimedJob, ctx: object) -> JobOutcome:
        self.seen.append(job)
        return Succeeded()


def worker(
    engine: Engine,
    clock: FakeClock,
    handler: Done,
    maintenance: JobMaintenance | None,
    **settings: object,
) -> JobWorker:
    cfg = JobWorkerSettings(queues={"q": 5}, **settings)  # type: ignore[arg-type]
    return JobWorker(
        JobLeaseStore(engine, clock),
        {"t": handler},
        cfg,
        clock,
        "w-main",
        BackoffPolicy(1.0, 10.0),
        maintenance,
    )


def test_the_worker_loop_reclaims_a_crashed_workers_job_and_runs_it(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    claim_one(store, "crashed-worker")  # never completes
    clock.advance(timedelta(seconds=120))
    handler = Done()
    maintenance = CountingMaintenance(app_engine, clock)
    worker(app_engine, clock, handler, maintenance).run_once()
    assert maintenance.calls == 1 and len(handler.seen) == 1
    assert job_rows(admin_engine)[0].status == "succeeded"


def test_reclaim_runs_at_most_once_per_interval(app_engine: Engine, clock: FakeClock) -> None:
    maintenance = CountingMaintenance(app_engine, clock)
    w = worker(app_engine, clock, Done(), maintenance, reclaim_interval_seconds=30)
    w.run_once()
    w.run_once()
    assert maintenance.calls == 1
    clock.advance(timedelta(seconds=29))
    w.run_once()
    assert maintenance.calls == 1
    clock.advance(timedelta(seconds=1))
    w.run_once()
    assert maintenance.calls == 2


def test_a_failing_reclaim_never_stops_the_loop_and_leaks_nothing(
    app_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enqueue_job(app_engine, clock, ids, 1, spec())
    handler = Done()
    maintenance = CountingMaintenance(app_engine, clock, fail=True)
    with caplog.at_level("WARNING"):
        worker(app_engine, clock, handler, maintenance).run_once()
    assert len(handler.seen) == 1
    assert "job_reclaim_failed" in caplog.text and "unavailable" not in caplog.text


def test_a_worker_without_maintenance_still_works(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, spec())
    handler = Done()
    worker(app_engine, clock, handler, None).run_once()
    assert len(handler.seen) == 1


def test_the_worker_applies_the_per_tenant_cap(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    seed(app_engine, clock, ids, {1: 4})
    handler = Done()
    worker(app_engine, clock, handler, None, max_running_per_tenant=2).run_once()
    assert len(handler.seen) == 2  # the first two ran and finished; the cap bounds one pass


# ---- gaps found by the mutation campaign -------------------------------------------------------


def test_the_capped_claim_serializes_on_a_per_queue_advisory_lock(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    """While another transaction holds the queue's claim lock, a capped claim must wait."""
    store = JobLeaseStore(app_engine, clock)
    seed(app_engine, clock, ids, {1: 2})
    done: list[int] = []
    with app_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('q', 0))"))
        worker = threading.Thread(
            target=lambda: done.append(len(store.claim("q", 5, "w", LEASE, max_per_tenant=1)))
        )
        worker.start()
        time.sleep(0.6)
        assert done == []  # blocked behind the lock holder
        holder.rollback()  # releases the transaction-level lock
        worker.join(timeout=10)
    assert done == [1]


def test_the_fence_rejects_another_tenants_job_even_when_row_security_is_bypassed(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    enqueue_job(app_engine, clock, ids, 1, spec())
    job = claim_one(store)
    # a superuser connection ignores RLS: only the predicate in the fence's own SQL stops tenant 2
    with pytest.raises(ConflictError, match="job_lease_lost"):
        verify(admin_engine, clock, job, n=2)
    verify(admin_engine, clock, job, n=1)  # and tenant 1 is still accepted there
