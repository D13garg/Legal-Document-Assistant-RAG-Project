"""Queue semantics under duplicates, concurrency, lease loss and failure (ADR-012)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine

from lda.platform.configuration.settings import JobWorkerSettings
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.queue import PostgresJobQueue
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.transaction import TenantTransaction
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ConflictError
from lda.shared.kernel.ids import JobId
from lda.shared.ports.jobs import ClaimedJob, JobOutcome, JobSpec, JsonValue, RetryAfter, Succeeded
from platform_support import enqueue_job, job_rows, tenant

LEASE = timedelta(seconds=30)


# ---- JobSpec validation ------------------------------------------------------------------------


@pytest.mark.parametrize("unique_key", ["", " ", "\t"])
def test_blank_unique_keys_are_rejected_because_they_would_swallow_later_jobs(
    unique_key: str,
) -> None:
    with pytest.raises(ValueError, match="unique_key"):
        JobSpec(queue="q", job_type="t", unique_key=unique_key)


def test_naive_run_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        JobSpec(queue="q", job_type="t", run_at=datetime(2026, 1, 1))


@pytest.mark.parametrize("kwargs", [{"queue": ""}, {"job_type": ""}, {"max_attempts": 0}])
def test_jobspec_required_fields(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"queue": "q", "job_type": "t"}
    with pytest.raises(ValueError):
        JobSpec(**{**base, **kwargs})  # type: ignore[arg-type]


# ---- duplicate unique_key ----------------------------------------------------------------------


def test_duplicate_enqueue_is_a_noop_and_keeps_the_original_job(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    first = JobSpec(queue="q", job_type="first", payload={"v": 1}, unique_key="k")
    again = JobSpec(queue="other", job_type="second", payload={"v": 2}, unique_key="k")
    assert enqueue_job(app_engine, clock, ids, 1, first) is not None
    assert enqueue_job(app_engine, clock, ids, 1, again) is None
    rows = job_rows(admin_engine)
    assert len(rows) == 1 and rows[0].payload == {"v": 1}


def test_unique_key_is_still_reserved_after_success_and_dead_letter(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    store = JobLeaseStore(app_engine, clock)
    for key, finish in (("done", "complete"), ("dead", "fail")):
        spec = JobSpec(queue="q", job_type=key, unique_key=key)
        assert enqueue_job(app_engine, clock, ids, 1, spec) is not None
        (job,) = store.claim("q", 1, "w", LEASE)
        if finish == "complete":
            assert store.complete(job.id, "w", job.fencing_token)
        else:
            assert store.fail(job.id, "w", job.fencing_token, "bad")
        assert (
            enqueue_job(app_engine, clock, ids, 1, spec) is None
        )  # event idempotency relies on this
    assert sorted(r.status for r in job_rows(admin_engine)) == ["dead", "succeeded"]


def test_jobs_without_unique_key_are_never_deduplicated(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    for _ in range(3):
        assert enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t")) is not None
    assert len(job_rows(admin_engine)) == 3


def test_duplicate_inside_one_transaction_is_deduplicated(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    spec = JobSpec(queue="q", job_type="t", unique_key="k")
    with TenantTransaction(app_engine, tenant(1)) as tx:
        queue = PostgresJobQueue(tx, clock, ids)
        assert queue.enqueue(spec) is not None and queue.enqueue(spec) is None
        tx.commit()
    assert len(job_rows(admin_engine)) == 1


def concurrent_duplicate(
    engine: Engine, clock: FakeClock, ids: SequentialIdGenerator, first_commits: bool
) -> tuple[bool, JobId | None]:
    spec = JobSpec(queue="q", job_type="t", unique_key="race")
    result: list[JobId | None] = []
    returned = threading.Event()

    def second() -> None:
        with TenantTransaction(engine, tenant(1)) as tx2:
            result.append(PostgresJobQueue(tx2, clock, ids).enqueue(spec))
            tx2.commit()
        returned.set()

    with TenantTransaction(engine, tenant(1)) as tx1:
        assert PostgresJobQueue(tx1, clock, ids).enqueue(spec) is not None
        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        blocked = not returned.wait(0.7)  # the competing insert must wait for tx1's outcome
        tx1.commit() if first_commits else tx1.rollback()
    thread.join(10)
    assert returned.is_set()
    return blocked, result[0]


def test_concurrent_duplicate_waits_then_becomes_a_noop_when_the_first_commits(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    blocked, second = concurrent_duplicate(app_engine, clock, ids, first_commits=True)
    assert blocked and second is None
    assert len(job_rows(admin_engine)) == 1


def test_concurrent_duplicate_succeeds_when_the_first_rolls_back(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    blocked, second = concurrent_duplicate(app_engine, clock, ids, first_commits=False)
    assert (
        blocked and second is not None
    )  # no job may be lost because a rolled-back one held the key
    assert len(job_rows(admin_engine)) == 1


# ---- claiming ----------------------------------------------------------------------------------


def test_concurrent_claimers_never_receive_the_same_job(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    for i in range(24):
        enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type=f"t{i}"))
    store = JobLeaseStore(app_engine, clock)
    claimed: list[list[ClaimedJob]] = [[] for _ in range(4)]

    def work(slot: int) -> None:
        while batch := store.claim("q", 3, f"w{slot}", LEASE):
            claimed[slot].extend(batch)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    ids_seen = [j.id for batch in claimed for j in batch]
    assert len(ids_seen) == 24 and len(set(ids_seen)) == 24


def test_payload_survives_the_round_trip_and_tenant_comes_from_the_row(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    payload: dict[str, JsonValue] = {"a": [1, {"b": None}], "c": "x", "d": True}
    enqueue_job(app_engine, clock, ids, 5, JobSpec(queue="q", job_type="t", payload=payload))
    (job,) = JobLeaseStore(app_engine, clock).claim("q", 1, "w", LEASE)
    assert dict(job.payload) == payload and job.tenant_id == tenant(5)


# ---- leases / heartbeat / retry ----------------------------------------------------------------


def test_claim_records_owner_and_expiry_and_complete_clears_them(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store = JobLeaseStore(app_engine, clock)
    (job,) = store.claim("q", 1, "w1", LEASE)
    (row,) = job_rows(admin_engine)
    assert (row.status, row.lease_owner, row.attempts) == ("running", "w1", 1)
    assert row.lease_expires_at == clock.now() + LEASE
    assert store.complete(job.id, "w1", job.fencing_token)
    (row,) = job_rows(admin_engine)
    assert (row.status, row.lease_owner, row.lease_expires_at) == ("succeeded", None, None)
    assert row.finished_at == clock.now()


def test_heartbeat_extends_the_lease_and_prevents_reclaim(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    (job,) = store.claim("q", 1, "w", LEASE)
    clock.advance(timedelta(seconds=25))
    assert store.heartbeat(job.id, "w", job.fencing_token, LEASE)
    clock.advance(timedelta(seconds=25))  # past the ORIGINAL expiry, inside the renewed one
    assert maintenance.reclaim_expired() == 0
    (row,) = job_rows(admin_engine)
    assert (
        row.status == "running"
        and row.lease_expires_at == clock.now() - timedelta(seconds=25) + LEASE
    )


def test_only_the_current_owner_can_heartbeat_complete_retry_or_fail(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store = JobLeaseStore(app_engine, clock)
    (job,) = store.claim("q", 1, "owner", LEASE)
    before = job_rows(admin_engine)
    assert not store.heartbeat(job.id, "intruder", job.fencing_token, LEASE)
    assert not store.complete(job.id, "intruder", job.fencing_token)
    assert not store.retry(
        job.id, "intruder", job.fencing_token, timedelta(0), "x", consumes_attempt=True
    )
    assert not store.fail(job.id, "intruder", job.fencing_token, "x")
    assert job_rows(admin_engine) == before


def test_a_stale_owner_cannot_act_after_the_job_was_reclaimed_and_reassigned(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", max_attempts=3))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    (first,) = store.claim("q", 1, "w1", LEASE)
    clock.advance(LEASE + timedelta(seconds=1))
    assert maintenance.reclaim_expired() == 1
    (second,) = store.claim("q", 1, "w2", LEASE)
    assert second.id == first.id and (first.attempt, second.attempt) == (1, 2)  # at-least-once
    assert not store.heartbeat(first.id, "w1", first.fencing_token, LEASE)
    assert not store.complete(first.id, "w1", first.fencing_token) and not store.fail(
        first.id, "w1", first.fencing_token, "x"
    )
    (row,) = job_rows(admin_engine)
    assert (row.status, row.lease_owner) == ("running", "w2")


def test_reclaim_requeues_when_attempts_remain_and_dead_letters_when_exhausted(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="spare", max_attempts=2))
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="last", max_attempts=1))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    store.claim("q", 2, "w", LEASE)
    clock.advance(LEASE + timedelta(seconds=1))
    assert maintenance.reclaim_expired() == 2
    by_status = {r.status: r for r in job_rows(admin_engine)}
    assert by_status["queued"].last_error_code == "lease_expired"
    assert by_status["queued"].run_at == clock.now() and by_status["queued"].lease_owner is None
    assert by_status["dead"].last_error_code == "lease_expired"
    assert by_status["dead"].finished_at == clock.now()


def test_finished_and_queued_jobs_are_never_reclaimed(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="done"))
    enqueue_job(
        app_engine,
        clock,
        ids,
        1,
        JobSpec(queue="q", job_type="waiting", run_at=clock.now() + timedelta(days=1)),
    )
    store = JobLeaseStore(app_engine, clock)
    (job,) = store.claim("q", 1, "w", LEASE)
    store.complete(job.id, "w", job.fencing_token)
    clock.advance(timedelta(days=2))
    assert JobMaintenance(app_engine, clock).reclaim_expired() == 0
    assert sorted(r.status for r in job_rows(admin_engine)) == ["queued", "succeeded"]


def test_attempt_neutral_retry_never_drives_attempts_below_zero(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store = JobLeaseStore(app_engine, clock)
    for _ in range(3):
        (job,) = store.claim("q", 1, "w", LEASE)
        assert job.attempt == 1  # claim +1, neutral retry -1: the budget is untouched
        store.retry(
            job.id,
            "w",
            job.fencing_token,
            timedelta(0),
            "provider_unavailable",
            consumes_attempt=False,
        )
    assert job_rows(admin_engine)[0].attempts == 0


def test_dead_jobs_are_not_claimable_and_only_dead_jobs_can_be_replayed(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)
    (job,) = store.claim("q", 1, "w", LEASE)
    assert not maintenance.replay(job.id)  # running: not replayable
    store.fail(job.id, "w", job.fencing_token, "bad")
    assert store.claim("q", 1, "w", LEASE) == []
    assert maintenance.replay(job.id)
    (row,) = job_rows(admin_engine)
    assert (row.status, row.attempts, row.last_error_code, row.finished_at) == (
        "queued",
        0,
        None,
        None,
    )
    assert not maintenance.replay(JobId(UUID(int=999)))  # unknown id


# ---- backoff -----------------------------------------------------------------------------------


def test_backoff_grows_exponentially_is_capped_and_never_overflows() -> None:
    policy = BackoffPolicy(base_seconds=5.0, cap_seconds=900.0, jitter=lambda: 1.0)
    delays = [policy.delay(n).total_seconds() for n in (0, 1, 2, 3, 4, 8, 9, 10, 100, 1100, 10**6)]
    assert delays == [5, 5, 10, 20, 40, 640, 900, 900, 900, 900, 900]


@pytest.mark.parametrize(("jitter", "factor"), [(0.0, 0.5), (0.5, 0.75), (1.0, 1.0)])
def test_backoff_jitter_stays_between_half_and_full_ceiling(jitter: float, factor: float) -> None:
    policy = BackoffPolicy(base_seconds=8.0, cap_seconds=100.0, jitter=lambda: jitter)
    assert policy.delay(3).total_seconds() == pytest.approx(32.0 * factor)


# ---- worker: heartbeat, lease loss, failure handling --------------------------------------------


class Gate:
    """Handler that blocks until released, so tests control when a job 'finishes'."""

    def __init__(self, outcome: JobOutcome | None = None) -> None:
        self.started, self.release = threading.Event(), threading.Event()
        self.outcome = outcome or Succeeded()

    def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
        self.started.set()
        assert self.release.wait(30)
        return self.outcome


def make_worker(
    engine: Engine, clock: FakeClock, handler: object, store: JobLeaseStore | None = None
) -> JobWorker:
    return JobWorker(
        store or JobLeaseStore(engine, clock),
        {"t": handler},  # type: ignore[dict-item]
        JobWorkerSettings(queues={"q": 1}, lease_seconds=5, heartbeat_seconds=1),
        clock,
        "w",
        BackoffPolicy(1, 10, lambda: 0.0),
    )


def run_in_thread(worker: JobWorker) -> threading.Thread:
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    return thread


def test_heartbeat_keeps_a_long_running_job_leased(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    gate = Gate()
    thread = run_in_thread(make_worker(app_engine, clock, gate))
    assert gate.started.wait(10)
    clock.advance(timedelta(seconds=60))  # the original 5 s lease is long gone by the clock
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        expires = job_rows(admin_engine)[0].lease_expires_at
        if expires is not None and expires > clock.now():
            break
        time.sleep(0.05)
    assert JobMaintenance(app_engine, clock).reclaim_expired() == 0  # the heartbeat renewed it
    gate.release.set()
    thread.join(10)
    assert job_rows(admin_engine)[0].status == "succeeded"


class FlakyHeartbeat(JobLeaseStore):
    def __init__(self, engine: Engine, clock: FakeClock) -> None:
        super().__init__(engine, clock)
        self.calls = 0

    def heartbeat(
        self, job_id: JobId, worker_id: str, fencing_token: int, lease: timedelta
    ) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("connection reset: PRIVILEGED-DETAIL")
        return super().heartbeat(job_id, worker_id, fencing_token, lease)


def test_a_failed_heartbeat_does_not_stop_later_renewals_and_leaks_no_detail(
    app_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t"))
    store = FlakyHeartbeat(app_engine, clock)
    gate = Gate()
    with caplog.at_level(logging.WARNING, logger="lda.jobs"):
        thread = run_in_thread(make_worker(app_engine, clock, gate, store))
        assert gate.started.wait(10)
        deadline = time.monotonic() + 10
        while store.calls < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        gate.release.set()
        thread.join(10)
    assert store.calls >= 2  # the thread survived the first failure and renewed again
    assert [r.getMessage() for r in caplog.records] == ["job_heartbeat_failed"]
    assert "PRIVILEGED" not in caplog.text


def test_a_worker_that_lost_its_lease_cannot_overwrite_the_new_owners_job(
    app_engine: Engine,
    admin_engine: Engine,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", max_attempts=3))
    store, maintenance = JobLeaseStore(app_engine, clock), JobMaintenance(app_engine, clock)

    class LosesLease:
        def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
            clock.advance(timedelta(seconds=10))  # lease (5 s) expires while we "work"
            assert maintenance.reclaim_expired() == 1
            assert len(store.claim("q", 1, "other-worker", LEASE)) == 1  # another worker takes over
            return Succeeded()  # at-least-once: both executed; only the owner may record the result

    with caplog.at_level(logging.WARNING, logger="lda.jobs"):
        make_worker(app_engine, clock, LosesLease()).run_once()
    (row,) = job_rows(admin_engine)
    assert (row.status, row.lease_owner, row.attempts) == ("running", "other-worker", 2)
    assert "job_lease_lost" in [r.getMessage() for r in caplog.records]


def test_handler_exceptions_record_only_a_stable_code_never_the_message(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", max_attempts=1))

    class Raises:
        def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
            raise ConflictError("document_locked", detail="CLIENT-NAME in detail")

    make_worker(app_engine, clock, Raises()).run_once()
    (row,) = job_rows(admin_engine)
    assert (row.status, row.last_error_code) == ("dead", "document_locked")  # attempts exhausted


def test_handlers_always_receive_a_system_context_for_the_jobs_own_tenant(
    app_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    enqueue_job(app_engine, clock, ids, 4, JobSpec(queue="q", job_type="t"))
    seen: list[object] = []

    class Capture:
        def handle(self, job: ClaimedJob, ctx: SystemContext) -> JobOutcome:
            seen.append(ctx)
            return RetryAfter(delay=timedelta(seconds=1), error_code="later")

    make_worker(app_engine, clock, Capture()).run_once()
    (ctx,) = seen
    assert isinstance(ctx, SystemContext) and ctx.tenant_id == tenant(4)
    assert ctx.purpose == "job:t" and ctx.job_id is not None


def test_worker_claims_no_more_than_the_configured_concurrency(
    app_engine: Engine, admin_engine: Engine, clock: FakeClock, ids: SequentialIdGenerator
) -> None:
    for i in range(3):
        enqueue_job(app_engine, clock, ids, 1, JobSpec(queue="q", job_type="t", unique_key=f"k{i}"))
    gate = Gate()
    thread = run_in_thread(make_worker(app_engine, clock, gate))  # queues={"q": 1}
    assert gate.started.wait(10)
    assert sorted(r.status for r in job_rows(admin_engine)) == ["queued", "queued", "running"]
    gate.release.set()
    thread.join(10)
