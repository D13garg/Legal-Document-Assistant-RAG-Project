"""Postgres `EvaluationStore` (ADR-041 D3): global, insert-only, content-free.

Not tenant-scoped: the data is synthetic evaluation evidence. Each call is one transaction; every
database failure is translated and chained away, because driver errors embed statement
parameters. Nothing here logs. The store never interprets a run: it returns stored text and the
stored hash, and the domain verifier judges them.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import BigInteger, Engine, Uuid, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lda.modules.evaluation.application.release_ports import (
    EvidenceSnapshot,
    StoredEligibility,
    StoredRun,
)
from lda.modules.evaluation.domain.codes import PERSISTENCE_FAILED, RUN_EXISTS
from lda.modules.evaluation.domain.release import EvaluationRecord
from lda.modules.evaluation.infrastructure.persistence.tables import (
    evaluation_runs as runs,
)
from lda.modules.evaluation.infrastructure.persistence.tables import (
    release_eligibility as eligibility,
)
from lda.shared.kernel.errors import ConflictError, InfrastructureError


def _run[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except SQLAlchemyError:
        raise InfrastructureError(PERSISTENCE_FAILED) from None


def _stored_run(row: tuple[object, ...]) -> StoredRun:
    sequence, run_id, text, sha = row
    return StoredRun(int(sequence), str(run_id), str(text), str(sha))  # type: ignore[call-overload]


def _stored_eligibility(row: tuple[object, ...]) -> StoredEligibility:
    sequence, eligibility_id, run_id, run_sha, config_id, recorded_at = row
    return StoredEligibility(
        int(sequence),  # type: ignore[call-overload]
        str(eligibility_id),
        str(run_id),
        str(run_sha),
        str(config_id),
        int(recorded_at),  # type: ignore[call-overload]
    )


def _same_uuid(left: object, right: object) -> bool:
    return UUID(str(left)) == UUID(str(right))


def _is_this_run(eligibility_row: tuple[object, ...], run: StoredRun) -> bool:
    """A run's identity is (run_id, record_sha256): the schema binds an eligibility record to both
    (composite foreign key). The insert only writes when the supplied hash matches the stored run;
    the read-back must hold to the same rule, or a matching run_id alone would turn a request
    describing a different run record into a successful replay."""
    return str(eligibility_row[3]) == run.record_sha256  # run_sha256 column


_RUN_COLUMNS = (runs.c.sequence, runs.c.run_id, runs.c.record_text, runs.c.record_sha256)
_ELIGIBILITY_COLUMNS = (
    eligibility.c.sequence,
    eligibility.c.eligibility_id,
    eligibility.c.run_id,
    eligibility.c.run_sha256,
    eligibility.c.config_id,
    eligibility.c.recorded_at,
)


class SqlEvaluationStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._reads = engine.execution_options(isolation_level="REPEATABLE READ")

    def append_run(self, record: EvaluationRecord) -> StoredRun:
        text = record.canonical()
        sha = record.sha256()
        statement = (
            pg_insert(runs)
            .values(
                run_id=UUID(record.run_id),
                config_id=record.config_id,
                dataset_id=record.dataset_id,
                dataset_version=record.dataset_version,
                dataset_fingerprint=record.dataset_fingerprint,
                evaluated_at=record.evaluated_at,
                passed=record.passed,
                record_text=text,
                record_sha256=sha,
            )
            # No conflict target, deliberately. A named target arbitrates only that one index; a
            # concurrent duplicate can reach the composite UNIQUE (run_id, ...) first and raise
            # 23505 instead of being skipped (Step 18 fix). Every unique constraint on this table
            # contains run_id, so any suppressed conflict means this run_id already exists, which
            # is exactly RUN_EXISTS. CHECK, foreign-key, type and connection errors are NOT
            # suppressed by ON CONFLICT and still surface as PERSISTENCE_FAILED.
            .on_conflict_do_nothing()
            .returning(runs.c.sequence)
        )

        def insert() -> int | None:
            with Session(self._engine) as session, session.begin():
                row = session.execute(statement).first()
                return None if row is None else int(row[0])

        sequence = _run(insert)
        if sequence is None:
            raise ConflictError(RUN_EXISTS)
        return StoredRun(sequence, record.run_id, text, sha)

    def snapshot(
        self, config_id: str, dataset_id: str, dataset_version: int, since: int
    ) -> EvidenceSnapshot:
        of_dataset = (
            runs.c.config_id == config_id,
            runs.c.dataset_id == dataset_id,
            runs.c.dataset_version == dataset_version,
        )

        def read() -> EvidenceSnapshot:
            with Session(self._reads) as session, session.begin():
                latest = session.execute(
                    select(*_RUN_COLUMNS)
                    .where(*of_dataset)
                    .order_by(runs.c.sequence.desc())
                    .limit(1)
                ).first()
                failed = session.execute(
                    select(func.count())
                    .select_from(runs)
                    .where(*of_dataset, runs.c.passed.is_(False), runs.c.evaluated_at >= since)
                ).scalar_one()
                entry = session.execute(
                    select(*_ELIGIBILITY_COLUMNS)
                    .where(eligibility.c.config_id == config_id)
                    .order_by(eligibility.c.sequence.desc())
                    .limit(1)
                ).first()
                return EvidenceSnapshot(
                    None if latest is None else _stored_run(tuple(latest)),
                    int(failed),
                    None if entry is None else _stored_eligibility(tuple(entry)),
                )

        return _run(read)

    def record_eligibility(
        self, eligibility_id: str, run: StoredRun, recorded_at: int
    ) -> tuple[StoredEligibility, bool]:
        """Copy the identifying columns from the run row itself, so the record can only describe
        the run it names; a missing run or a hash that differs inserts nothing and fails."""
        source = select(
            literal(UUID(eligibility_id), Uuid),
            runs.c.run_id,
            runs.c.config_id,
            runs.c.dataset_fingerprint,
            runs.c.record_sha256,
            runs.c.passed,
            literal(recorded_at, BigInteger),
        ).where(runs.c.run_id == UUID(run.run_id), runs.c.record_sha256 == run.record_sha256)
        statement = (
            pg_insert(eligibility)
            .from_select(
                [
                    "eligibility_id",
                    "run_id",
                    "config_id",
                    "dataset_fingerprint",
                    "run_sha256",
                    "run_passed",
                    "recorded_at",
                ],
                source,
            )
            # No conflict target, deliberately: a retry that repeats the same eligibility_id would
            # otherwise race on the eligibility_id UNIQUE and raise 23505 (Step 18 fix). A skipped
            # insert is resolved by the single read below, which tells apart an id that already
            # belongs to THIS run (replay), an id that belongs to ANOTHER run (failure, even when
            # this run has its own record), and a fresh id for a run that already has a record
            # (replay of that record). In every case the record must carry the supplied run's
            # hash, as the insert itself requires.
            .on_conflict_do_nothing()
            .returning(eligibility.c.sequence)
        )

        def write() -> tuple[StoredEligibility, bool]:
            with Session(self._engine) as session, session.begin():
                inserted = session.execute(statement).first() is not None
                # ONE statement, so both candidates come from the same snapshot. A skipped insert
                # means the conflicting row is already committed (ON CONFLICT waits for an
                # in-progress writer to finish), so it is visible to this READ COMMITTED read.
                candidates = [
                    tuple(r)
                    for r in session.execute(
                        select(*_ELIGIBILITY_COLUMNS).where(
                            or_(
                                eligibility.c.eligibility_id == UUID(eligibility_id),
                                eligibility.c.run_id == UUID(run.run_id),
                            )
                        )
                    ).all()
                ]
                by_id = next((r for r in candidates if _same_uuid(r[1], eligibility_id)), None)
                if by_id is not None:
                    if not _same_uuid(by_id[2], run.run_id) or not _is_this_run(by_id, run):
                        # The supplied id already belongs to a DIFFERENT run (or to a record of
                        # this run_id bound to a different hash). Never a replay, even if this
                        # run has a record of its own.
                        raise InfrastructureError(PERSISTENCE_FAILED)
                    return _stored_eligibility(by_id), not inserted
                by_run = next((r for r in candidates if _same_uuid(r[2], run.run_id)), None)
                if by_run is None or not _is_this_run(by_run, run):
                    raise InfrastructureError(PERSISTENCE_FAILED)
                return _stored_eligibility(by_run), True  # a fresh id: the run's own record stands

        return _run(write)
