"""Duplicate-write handling of the evaluation store under real PostgreSQL concurrency (Step 18).

Defect fixed: both inserts named `run_id` as the `ON CONFLICT` arbiter. A named arbiter covers only
that one index, so a concurrent duplicate could trip a DIFFERENT unique constraint first (the
composite `UNIQUE (run_id, ...)` on runs; `eligibility_id` on eligibility records) and raise
SQLSTATE 23505, which the store translated to `PERSISTENCE_FAILED` instead of the documented
`RUN_EXISTS` conflict or an eligibility replay.

What these tests do and do not prove. The statement-shape test is deterministic. The barrier
tests are statistical: the race cannot be forced from SQL, so they repeat many synchronized
rounds against embedded PostgreSQL (`pgserver`, not a CI service container). A pass is strong
evidence, not a proof, and the eligibility race is rare (about 0.1% of rounds before the fix).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from functools import partial
from typing import cast

import pytest
from sqlalchemy import Engine, event, text

from lda.modules.evaluation.api import PERSISTENCE_FAILED, RUN_EXISTS
from lda.modules.evaluation.infrastructure.persistence.store import SqlEvaluationStore
from lda.modules.evaluation.tests.integration.test_release_persistence import (
    record_for,
    rid,
    scalar,
)
from lda.shared.kernel.errors import ConflictError, InfrastructureError

THREADS = 12
RUN_ROUNDS = 200  # ~1.8% of unsynchronized-fix rounds failed before the fix
ELIGIBILITY_ROUNDS = 1_000  # ~0.1% of rounds failed before the fix


def barrier_calls[T](calls: Sequence[Callable[[], T]]) -> list[T | Exception]:
    """Release one thread per call at the same instant so the database work genuinely overlaps."""
    barrier = threading.Barrier(len(calls))
    results: list[T | Exception | None] = [None] * len(calls)

    def target(i: int) -> None:
        barrier.wait()
        try:
            results[i] = calls[i]()
        except Exception as exc:  # the outcome type is what is asserted
            results[i] = exc

    threads = [threading.Thread(target=target, args=(i,)) for i in range(len(calls))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return cast("list[T | Exception]", results)


def barrier_run[T](count: int, call: Callable[[], T]) -> list[T | Exception]:
    return barrier_calls([call] * count)


# ---- deterministic: the statement shape that removes the race --------------------------------


def test_neither_insert_names_a_conflict_arbiter(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    executed: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith("INSERT INTO EVALUATION."):
            executed.append(statement)

    event.listen(app_engine, "before_cursor_execute", capture)
    try:
        run = store.append_run(record_for(10))
        store.record_eligibility(rid(11), run, 1_500)
    finally:
        event.remove(app_engine, "before_cursor_execute", capture)

    assert len(executed) == 2
    for statement in executed:
        assert "ON CONFLICT DO NOTHING" in statement
        assert "ON CONFLICT (" not in statement  # a named arbiter is the defect


# ---- deterministic: the contract around the suppressed conflicts -----------------------------


def test_a_repeated_run_id_with_different_content_is_still_a_conflict_and_changes_nothing(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    first = store.append_run(record_for(20, at=1_000))
    with pytest.raises(ConflictError) as error:
        store.append_run(record_for(20, at=2_000))  # same run_id, different evaluated_at and text
    assert error.value.args == (RUN_EXISTS,)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 1
    assert (
        scalar(app_engine, "SELECT record_text FROM evaluation.evaluation_runs")
        == first.record_text
    )


def test_an_unrelated_database_error_is_not_suppressed_or_reported_as_a_conflict(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    record = record_for(21)
    object.__setattr__(
        record, "evaluated_at", 2**70
    )  # out of range for bigint: not a unique conflict
    with pytest.raises(InfrastructureError) as error:
        store.append_run(record)
    assert not isinstance(error.value, ConflictError)
    assert error.value.args == (PERSISTENCE_FAILED,)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 0


def test_repeating_an_eligibility_id_for_the_same_run_is_a_replay(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    run = store.append_run(record_for(30))
    first, replayed = store.record_eligibility(rid(31), run, 1_500)
    again, replayed_again = store.record_eligibility(rid(31), run, 1_600)  # same id, same run
    assert not replayed and replayed_again and again == first
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 1


def test_reusing_an_eligibility_id_for_a_different_run_fails_and_keeps_the_original(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    run_a = store.append_run(record_for(40))
    run_b = store.append_run(record_for(41))
    original, _ = store.record_eligibility(rid(42), run_a, 1_500)

    with pytest.raises(InfrastructureError) as error:  # the id is taken by another run
        store.record_eligibility(rid(42), run_b, 1_600)

    assert error.value.args == (PERSISTENCE_FAILED,)
    assert not isinstance(error.value, ConflictError)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 1
    stored = store.record_eligibility(rid(43), run_a, 1_700)  # run A's record is untouched
    assert stored == (original, True)
    with app_engine.connect() as conn:
        owners = conn.execute(text("SELECT run_id::text FROM evaluation.release_eligibility"))
        assert [r[0] for r in owners] == [run_a.run_id]


def test_reusing_an_eligibility_id_for_a_run_that_already_has_its_own_record_fails(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    """The edge the previous test cannot reach: B already has a record, then B is asked to record
    A's eligibility_id. The skipped insert must not be mistaken for a replay of B's own record."""
    run_a = store.append_run(record_for(50))
    run_b = store.append_run(record_for(51))
    e_a, _ = store.record_eligibility(rid(52), run_a, 1_500)  # E-A belongs to A
    e_b, _ = store.record_eligibility(rid(53), run_b, 1_600)  # E-B belongs to B

    with pytest.raises(InfrastructureError) as error:
        store.record_eligibility(rid(52), run_b, 1_700)  # E-A offered for B

    assert error.value.args == (PERSISTENCE_FAILED,)  # not a replay, not a conflict
    assert not isinstance(error.value, ConflictError)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 2
    with app_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT eligibility_id::text, run_id::text, recorded_at "
                "FROM evaluation.release_eligibility ORDER BY sequence"
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        (e_a.eligibility_id, run_a.run_id, 1_500),
        (e_b.eligibility_id, run_b.run_id, 1_600),
    ]  # E-A still A's, E-B still B's, nothing rewritten
    assert store.record_eligibility(rid(54), run_b, 1_800) == (
        e_b,
        True,
    )  # B's own replay still works


def table_rows(engine: Engine, sql: str) -> list[tuple[object, ...]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql)).all()]


RUN_ROWS = "SELECT * FROM evaluation.evaluation_runs ORDER BY sequence"
ELIGIBILITY_ROWS = "SELECT * FROM evaluation.release_eligibility ORDER BY sequence"


@pytest.mark.parametrize("wrong_hash", ["zeros", "another-runs-real-hash"])
@pytest.mark.parametrize("same_eligibility_id", [True, False], ids=["same-id", "fresh-id"])
def test_a_mismatched_run_hash_is_never_a_replay_even_when_the_run_has_a_record(
    store: SqlEvaluationStore, app_engine: Engine, wrong_hash: str, same_eligibility_id: bool
) -> None:
    """The run's identity is (run_id, record_sha256): the schema binds an eligibility record to
    both. A matching run_id with a different hash describes a different run record, so the
    existing record must not be returned as a replay of this request."""
    run = store.append_run(record_for(80))
    other = store.append_run(record_for(81))
    first, _ = store.record_eligibility(rid(82), run, 1_500)
    runs_before = table_rows(app_engine, RUN_ROWS)
    eligibility_before = table_rows(app_engine, ELIGIBILITY_ROWS)
    bad_sha = "0" * 64 if wrong_hash == "zeros" else other.record_sha256
    mismatched = replace(run, record_sha256=bad_sha)

    with pytest.raises(InfrastructureError) as error:
        store.record_eligibility(rid(82) if same_eligibility_id else rid(83), mismatched, 1_600)

    assert error.value.args == (PERSISTENCE_FAILED,)  # the existing contract, not a conflict
    assert not isinstance(error.value, ConflictError)
    assert table_rows(app_engine, RUN_ROWS) == runs_before  # runs unchanged
    assert table_rows(app_engine, ELIGIBILITY_ROWS) == eligibility_before  # nothing inserted
    assert store.record_eligibility(rid(84), run, 1_700) == (first, True)  # the true run replays


# ---- statistical: synchronized concurrent duplicates against real PostgreSQL ------------------


def test_synchronized_duplicate_run_ids_always_store_exactly_one(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    for n in range(RUN_ROUNDS):
        results = barrier_run(THREADS, partial(store.append_run, record_for(1_000 + n)))
        failures = [r for r in results if isinstance(r, Exception)]
        assert len(results) - len(failures) == 1, (n, results)
        assert all(isinstance(f, ConflictError) for f in failures), (n, failures)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == RUN_ROUNDS


def test_synchronized_retries_with_one_eligibility_id_all_succeed_as_one_record(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    for n in range(ELIGIBILITY_ROUNDS):
        run = store.append_run(record_for(10_000 + n))
        attempt = partial(store.record_eligibility, rid(500_000 + n), run, 1_500)
        outcomes = barrier_run(THREADS, attempt)
        errors = [o for o in outcomes if isinstance(o, Exception)]
        records = [o for o in outcomes if not isinstance(o, Exception)]
        assert not errors, (n, errors)
        assert sum(1 for _, replayed in records if not replayed) == 1, n
        assert len({stored.eligibility_id for stored, _ in records}) == 1, n
    assert (
        scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility")
        == ELIGIBILITY_ROUNDS
    )


def test_synchronized_reuse_of_another_runs_eligibility_id_always_fails(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    run_a = store.append_run(record_for(60_000))
    run_b = store.append_run(record_for(60_001))
    e_a, _ = store.record_eligibility(rid(60_002), run_a, 1_500)
    e_b, _ = store.record_eligibility(rid(60_003), run_b, 1_600)

    outcomes = barrier_run(THREADS, partial(store.record_eligibility, rid(60_002), run_b, 1_700))

    assert all(isinstance(o, InfrastructureError) for o in outcomes), outcomes
    assert all(o.args == (PERSISTENCE_FAILED,) for o in outcomes if isinstance(o, Exception))
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 2
    assert store.record_eligibility(rid(60_004), run_a, 1_800) == (e_a, True)
    assert store.record_eligibility(rid(60_005), run_b, 1_800) == (e_b, True)


MIXED_ROUNDS = 150


def test_synchronized_reuse_never_succeeds_while_fresh_ids_for_the_same_run_converge(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    """Run B has no record yet. Half the callers offer A's eligibility_id for B (must always
    fail); half offer their own fresh ids for B (must always succeed: one record plus replays)."""
    half = THREADS // 2
    for n in range(MIXED_ROUNDS):
        base = 100_000 + n * 100
        run_a = store.append_run(record_for(base))
        run_b = store.append_run(record_for(base + 1))
        e_a, _ = store.record_eligibility(rid(base + 2), run_a, 1_500)
        reuse = [partial(store.record_eligibility, rid(base + 2), run_b, 1_700)] * half
        fresh = [
            partial(store.record_eligibility, rid(base + 10 + k), run_b, 1_700) for k in range(half)
        ]
        outcomes = barrier_calls([c for pair in zip(reuse, fresh, strict=True) for c in pair])

        assert all(isinstance(o, InfrastructureError) for o in outcomes[0::2]), (n, outcomes)
        records = [o for o in outcomes[1::2] if not isinstance(o, Exception)]
        assert len(records) == half, (n, outcomes)
        assert len({stored.eligibility_id for stored, _ in records}) == 1, n
        assert e_a.eligibility_id not in {stored.eligibility_id for stored, _ in records}, n
        assert sum(1 for _, replayed in records if not replayed) == 1, n
    assert (
        scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility")
        == MIXED_ROUNDS * 2
    )


HASH_ROUNDS = 100


def test_synchronized_mismatched_hashes_never_succeed_while_correct_retries_converge(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    """Callers with a wrong hash race callers with the right one for the same run (fresh ids):
    the wrong ones must always fail, the right ones must always succeed as one record."""
    half = THREADS // 2
    for n in range(HASH_ROUNDS):
        run = store.append_run(record_for(200_000 + n))
        wrong = replace(run, record_sha256="f" * 64)
        bad = [
            partial(store.record_eligibility, rid(700_000 + n * 40 + k), wrong, 1_700)
            for k in range(half)
        ]
        good = [
            partial(store.record_eligibility, rid(700_000 + n * 40 + 20 + k), run, 1_700)
            for k in range(half)
        ]
        outcomes = barrier_calls([c for pair in zip(bad, good, strict=True) for c in pair])

        assert all(isinstance(o, InfrastructureError) for o in outcomes[0::2]), (n, outcomes)
        records = [o for o in outcomes[1::2] if not isinstance(o, Exception)]
        assert len(records) == half, (n, outcomes)
        assert len({stored.eligibility_id for stored, _ in records}) == 1, n
        assert {stored.run_sha256 for stored, _ in records} == {run.record_sha256}, n
        assert sum(1 for _, replayed in records if not replayed) == 1, n
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == HASH_ROUNDS
