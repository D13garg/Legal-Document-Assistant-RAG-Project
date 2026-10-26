"""Real PostgreSQL (embedded `pgserver` unless LDA_TEST_POSTGRES_URL is set; this is NOT a CI
service container): the evaluation schema's insert-only enforcement, database-checked integrity,
the eligibility foreign key, idempotency, concurrency and the full gate over a real store
(ADR-041 D3, D4)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from lda.modules.evaluation.api import (
    ELIGIBILITY_INCONSISTENT,
    ELIGIBILITY_NO_EVIDENCE,
    PERSISTENCE_FAILED,
    RELEASE_DATASET,
    RUN_EXISTS,
    CheckEligibility,
    EffectiveConfiguration,
    EvaluateConfiguration,
    EvaluationRecord,
    Observation,
    Outcome,
    RecordEligibility,
    RunEvaluation,
    default_metrics,
    release_criteria,
)
from lda.modules.evaluation.infrastructure.persistence.store import SqlEvaluationStore
from lda.modules.evaluation.testing import (
    PerfectTarget,
    ScriptedTarget,
    build_effective,
    default_limits,
)
from lda.modules.evaluation.tests.application.test_release_use_cases import failing_target
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConflictError, InfrastructureError

START = datetime(2026, 1, 1, 12, tzinfo=UTC)
CRITERIA = release_criteria(3_600)


def rid(n: int) -> str:
    return str(UUID(int=n))


def record_for(
    n: int,
    target: object | None = None,
    at: int = 1_000,
    effective: EffectiveConfiguration | None = None,
) -> EvaluationRecord:
    effective = effective or build_effective()
    runner = RunEvaluation(target or PerfectTarget(), default_metrics(), default_limits())  # type: ignore[arg-type]
    from lda.modules.evaluation.application.dto import RunEvaluationCommand

    report = runner.execute(
        RunEvaluationCommand(RELEASE_DATASET, effective.configuration(), CRITERIA.policy)
    )
    return EvaluationRecord(
        rid(n), at, effective.configuration(), RELEASE_DATASET.dataset_id,
        RELEASE_DATASET.version, RELEASE_DATASET.fingerprint(), CRITERIA.policy, report,
    )  # fmt: skip


def failing_record(n: int, at: int = 1_000) -> EvaluationRecord:
    script = {c.case_id: Observation(Outcome.INSUFFICIENT_EVIDENCE) for c in RELEASE_DATASET.cases}
    return record_for(n, ScriptedTarget(script), at)


def scalar(engine: Engine, sql: str, **params: object) -> object:
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


# ---- round trip, idempotency, content-free ---------------------------------------------------


def test_a_run_round_trips_with_the_hash_the_domain_computed(store: SqlEvaluationStore) -> None:
    record = record_for(1)
    stored = store.append_run(record)
    assert (stored.run_id, stored.record_text, stored.record_sha256) == (
        record.run_id,
        record.canonical(),
        record.sha256(),
    )
    snap = store.snapshot(record.config_id, RELEASE_DATASET.dataset_id, 1, 0)
    assert snap.latest_run == stored and snap.failed_since == 0 and snap.latest_eligibility is None
    assert EvaluationRecord.from_canonical(snap.latest_run.record_text) == record


def test_a_repeated_run_id_is_a_conflict_and_stores_nothing_new(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    store.append_run(record_for(2))
    with pytest.raises(ConflictError) as error:
        store.append_run(record_for(2, at=2_000))
    assert error.value.args[0] == RUN_EXISTS
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 1


def test_stored_rows_hold_no_question_text(store: SqlEvaluationStore, app_engine: Engine) -> None:
    store.append_run(record_for(3))
    with app_engine.connect() as conn:
        rows = conn.execute(text("SELECT record_text FROM evaluation.evaluation_runs")).all()
    blob = " ".join(r[0] for r in rows)
    assert all(c.question.reveal() not in blob for c in RELEASE_DATASET.cases)


def test_database_failures_are_translated_and_leak_nothing(database) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine

    url = database.app_url.replace("/lda_test_evaluation", "/no_such_database")
    dead = SqlEvaluationStore(create_engine(url))
    record = record_for(6)
    for call in (
        lambda: dead.append_run(record),
        lambda: dead.snapshot(record.config_id, "d", 1, 0),
        lambda: dead.record_eligibility(rid(1), _stored(record), 1),
    ):
        with pytest.raises(InfrastructureError) as error:
            call()
        assert error.value.args == (PERSISTENCE_FAILED,)
        assert error.value.__cause__ is None and error.value.__suppress_context__
        assert "no_such_database" not in repr(error.value) and "lda_app" not in repr(error.value)


def _stored(record: EvaluationRecord):  # type: ignore[no-untyped-def]
    from lda.modules.evaluation.api import StoredRun

    return StoredRun(1, record.run_id, record.canonical(), record.sha256())


# ---- the database, not the application, enforces insert-only ---------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evaluation.evaluation_runs SET passed = passed",
        "DELETE FROM evaluation.evaluation_runs",
        "TRUNCATE evaluation.evaluation_runs",
        "UPDATE evaluation.release_eligibility SET run_passed = run_passed",
        "DELETE FROM evaluation.release_eligibility",
        "TRUNCATE evaluation.release_eligibility",
    ],
)
def test_the_runtime_role_cannot_update_delete_or_truncate(
    store: SqlEvaluationStore, app_engine: Engine, sql: str
) -> None:
    store.append_run(record_for(10))
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text(sql))
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 1


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE evaluation.evaluation_runs SET evaluated_at = evaluated_at",
        "DELETE FROM evaluation.evaluation_runs",
        "TRUNCATE evaluation.evaluation_runs, evaluation.release_eligibility",
    ],
)
def test_even_the_owner_is_stopped_by_the_row_triggers(
    store: SqlEvaluationStore, admin_engine: Engine, sql: str
) -> None:
    store.append_run(record_for(11))
    with pytest.raises(DBAPIError) as error, admin_engine.begin() as conn:
        conn.execute(text(sql))
    assert "insert-only" in str(error.value)


# ---- database-checked integrity ---------------------------------------------------------------

INSERT = (
    "INSERT INTO evaluation.evaluation_runs (run_id, config_id, dataset_id, dataset_version, "
    "dataset_fingerprint, evaluated_at, passed, record_text, record_sha256) "
    "VALUES (:run_id, :config_id, :dataset_id, :dataset_version, :fp, :at, :passed, :text, :sha)"
)


def raw_insert(engine: Engine, record: EvaluationRecord, **override: object) -> None:
    params: dict[str, object] = {
        "run_id": record.run_id,
        "config_id": record.config_id,
        "dataset_id": record.dataset_id,
        "dataset_version": record.dataset_version,
        "fp": record.dataset_fingerprint,
        "at": record.evaluated_at,
        "passed": record.passed,
        "text": record.canonical(),
        "sha": record.sha256(),
    } | override
    with engine.begin() as conn:
        conn.execute(text(INSERT), params)


def test_a_well_formed_raw_insert_is_accepted(app_engine: Engine) -> None:
    raw_insert(app_engine, record_for(20))  # proves the tamper cases below fail for their reason


@pytest.mark.parametrize(
    "override",
    [
        {"sha": "0" * 64},  # hash does not match text
        {"config_id": "rag-" + "1" * 64},  # column disagrees with the text
        {"dataset_id": "other"},
        {"dataset_version": 2},
        {"fp": "2" * 64},
        {"at": 999},
        {"passed": False},  # claims fail for a passing text
        {"run_id": rid(999)},  # text names another run
        {"text": "not json", "sha": "0" * 64},
        {"config_id": "not-a-config-id"},
        {"sha": "ABC"},
    ],
)
def test_the_database_refuses_a_run_whose_columns_text_or_hash_disagree(
    app_engine: Engine, override: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError):
        raw_insert(app_engine, record_for(21), **override)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 0


def test_edited_text_with_a_recomputed_hash_passes_the_database_but_not_the_verifier(
    store: SqlEvaluationStore, admin_engine: Engine, app_engine: Engine
) -> None:
    """The strongest tamper: someone with owner rights rewrites a failing run as passing, fixes the
    column and recomputes the hash. The database cannot know; the verifier does."""
    from lda.modules.evaluation.domain.release import sha256_text

    effective = build_effective()
    failing = failing_record(30)
    store.append_run(failing)
    body = json.loads(failing.canonical())
    body["report"]["passed"] = True
    body["report"]["failures"] = []
    text_ = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with admin_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text(
                "UPDATE evaluation.evaluation_runs SET passed = true, record_text = :t, "
                "record_sha256 = :h WHERE run_id = :r"
            ),
            {"t": text_, "h": sha256_text(text_), "r": failing.run_id},
        )
    clock = FakeClock(START.replace(year=1970, month=1, day=1, hour=0) + timedelta(seconds=1_000))
    decision = CheckEligibility(CRITERIA, store, clock).execute(effective)
    assert not decision.eligible and ELIGIBILITY_INCONSISTENT in decision.failures
    outcome = RecordEligibility(CRITERIA, store, clock, SequentialIdGenerator()).execute(effective)
    assert not outcome.decision.eligible
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 0


def test_the_owner_cannot_rewrite_text_without_the_hash_check_firing(
    store: SqlEvaluationStore, admin_engine: Engine
) -> None:
    store.append_run(record_for(31))
    with pytest.raises(DBAPIError) as error, admin_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("UPDATE evaluation.evaluation_runs SET record_text = record_text || ' '"))
    assert "check" in str(error.value).lower()


# ---- eligibility: foreign key, uniqueness, idempotency ---------------------------------------


def test_eligibility_is_recorded_once_per_run_and_replays(store: SqlEvaluationStore) -> None:
    record = record_for(40)
    run = store.append_run(record)
    first, replayed = store.record_eligibility(rid(100), run, 1_500)
    assert not replayed and first.run_id == run.run_id and first.config_id == record.config_id
    again, replayed = store.record_eligibility(rid(101), run, 1_600)
    assert replayed and again == first  # the first record stands; the retry id is not stored
    snap = store.snapshot(record.config_id, RELEASE_DATASET.dataset_id, 1, 0)
    assert snap.latest_eligibility == first


def test_eligibility_cannot_be_recorded_for_a_failing_run(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    run = store.append_run(failing_record(41))
    with pytest.raises((InfrastructureError, SQLAlchemyError)):
        store.record_eligibility(rid(110), run, 1_500)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 0


def test_eligibility_for_an_unknown_run_or_wrong_hash_stores_nothing(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    record = record_for(42)
    run = store.append_run(record)
    with pytest.raises(InfrastructureError):
        store.record_eligibility(rid(111), replace(run, record_sha256="0" * 64), 1_500)
    with pytest.raises(InfrastructureError):
        store.record_eligibility(rid(112), replace(run, run_id=rid(404)), 1_500)
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 0


@pytest.mark.parametrize(
    "column_sql",
    [
        "config_id = 'rag-' || repeat('3', 64)",
        "dataset_fingerprint = repeat('4', 64)",
        "run_sha256 = repeat('5', 64)",
        "run_passed = false",
    ],
)
def test_the_foreign_key_refuses_an_eligibility_row_that_misdescribes_its_run(
    store: SqlEvaluationStore, app_engine: Engine, column_sql: str
) -> None:
    record = record_for(43)
    store.append_run(record)
    column, _, value = column_sql.partition(" = ")
    values = {
        "config_id": "config_id",
        "dataset_fingerprint": "dataset_fingerprint",
        "run_sha256": "record_sha256",
        "run_passed": "passed",
    } | {column: value}  # fixed test fragments only, never input
    sql = (
        "INSERT INTO evaluation.release_eligibility (eligibility_id, run_id, config_id, "  # noqa: S608
        "dataset_fingerprint, run_sha256, run_passed, recorded_at) "
        f"SELECT :e, run_id, {values['config_id']}, {values['dataset_fingerprint']}, "
        f"{values['run_sha256']}, {values['run_passed']}, 1 "
        "FROM evaluation.evaluation_runs WHERE run_id = :r"
    )
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text(sql), {"e": rid(120), "r": record.run_id})


# ---- snapshot semantics -----------------------------------------------------------------------


def test_the_snapshot_returns_the_latest_run_recent_failures_and_the_latest_record(
    store: SqlEvaluationStore,
) -> None:
    effective = build_effective()
    store.append_run(failing_record(50, at=100))
    store.append_run(failing_record(51, at=900))
    good = store.append_run(record_for(52, at=950))
    ds = RELEASE_DATASET.dataset_id
    snap = store.snapshot(effective.config_id, ds, 1, since=500)
    assert snap.latest_run == good and snap.failed_since == 1  # the t=100 failure is outside
    assert store.snapshot(effective.config_id, ds, 1, since=0).failed_since == 2
    assert store.snapshot(effective.config_id, ds, 1, since=901).failed_since == 0
    assert store.snapshot(effective.config_id, "other", 1, 0).latest_run is None
    assert store.snapshot(effective.config_id, ds, 2, 0).latest_run is None
    other = build_effective(retrieval__max_results=19).config_id
    assert store.snapshot(other, ds, 1, 0).latest_run is None


# ---- concurrency ------------------------------------------------------------------------------


def run_threads(work: list) -> list:  # type: ignore[type-arg]
    results: list = [None] * len(work)  # type: ignore[type-arg]

    def target(i: int) -> None:
        try:
            results[i] = work[i]()
        except Exception as exc:
            results[i] = exc

    threads = [threading.Thread(target=target, args=(i,)) for i in range(len(work))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_runs_are_all_stored_with_distinct_sequences(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    results = run_threads([lambda n=n: store.append_run(record_for(200 + n)) for n in range(12)])
    assert all(not isinstance(r, Exception) for r in results), results
    assert len({r.sequence for r in results}) == 12
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 12


def test_concurrent_identical_run_ids_store_exactly_one(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    results = run_threads([lambda: store.append_run(record_for(300)) for _ in range(8)])
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, Exception))
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 1


def test_concurrent_eligibility_attempts_on_one_run_make_one_record(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    run = store.append_run(record_for(400))
    results = run_threads(
        [lambda n=n: store.record_eligibility(rid(500 + n), run, 1_500) for n in range(10)]
    )
    assert all(not isinstance(r, Exception) for r in results), results
    assert sum(1 for _, replayed in results if not replayed) == 1
    assert len({e.eligibility_id for e, _ in results}) == 1
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 1


# ---- the whole gate over a real store --------------------------------------------------------


def gate(store: SqlEvaluationStore, clock: FakeClock, target: object | None = None):  # type: ignore[no-untyped-def]
    ids = SequentialIdGenerator()
    runner = RunEvaluation(target or PerfectTarget(), default_metrics(), default_limits())  # type: ignore[arg-type]
    return (
        EvaluateConfiguration(runner, RELEASE_DATASET, CRITERIA, store, clock, ids),
        RecordEligibility(CRITERIA, store, clock, ids),
        CheckEligibility(CRITERIA, store, clock),
    )


def test_the_full_gate_on_postgres_including_staleness_and_a_changed_configuration(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    clock, effective = FakeClock(START), build_effective()
    evaluate, record, check = gate(store, clock)
    assert check.execute(effective).failures == (ELIGIBILITY_NO_EVIDENCE,)
    assert evaluate.execute(effective).passed
    assert record.execute(effective).decision.eligible
    assert check.execute(effective).eligible
    assert not check.execute(build_effective(generation__max_statements=19)).eligible
    clock.advance(timedelta(seconds=3_600))
    assert check.execute(effective).eligible
    clock.advance(timedelta(seconds=1))
    assert not check.execute(effective).eligible
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 1


def test_a_failing_run_is_stored_but_never_eligible_on_postgres(
    store: SqlEvaluationStore, app_engine: Engine
) -> None:
    clock, effective = FakeClock(START), build_effective()
    evaluate, record, check = gate(store, clock, failing_target())
    assert not evaluate.execute(effective).passed
    assert not record.execute(effective).decision.eligible and not check.execute(effective).eligible
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.evaluation_runs") == 1
    assert scalar(app_engine, "SELECT count(*) FROM evaluation.release_eligibility") == 0


# ---- migration --------------------------------------------------------------------------------


def test_the_migration_downgrades_and_upgrades_again(database) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import create_engine

    from lda.modules.evaluation.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
    from lda.platform.database import migrate

    migrate.downgrade(
        database.admin_url, target="evaluation@base", extra_locations=[MIGRATIONS_VERSIONS],
        app_role="lda_app_evaluation",
    )  # fmt: skip
    admin = create_engine(database.admin_url)
    try:
        assert scalar(admin, "SELECT count(*) FROM pg_namespace WHERE nspname = 'evaluation'") == 0
    finally:
        admin.dispose()
    migrate.upgrade(
        database.admin_url, extra_locations=[MIGRATIONS_VERSIONS], app_role="lda_app_evaluation"
    )
    admin = create_engine(database.admin_url)
    try:
        assert scalar(admin, "SELECT count(*) FROM pg_namespace WHERE nspname = 'evaluation'") == 1
    finally:
        admin.dispose()
