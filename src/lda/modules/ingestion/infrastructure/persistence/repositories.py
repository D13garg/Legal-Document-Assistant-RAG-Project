from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Row, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import Executable

from lda.modules.ingestion.application.dto.runs import IngestionMetrics, StageMetric
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.modules.ingestion.domain.run import (
    OUTCOME_STATES,
    IngestionRun,
    RunStatus,
    StageRun,
    StageStatus,
)
from lda.modules.ingestion.infrastructure.persistence.tables import runs, stage_runs
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import TenantId

_FAILED = "ingestion_persistence_failed"


def _run[T](action: Callable[[], T]) -> T:
    """Driver errors embed statement parameters: translate and chain them away."""
    try:
        return action()
    except SQLAlchemyError:
        raise InfrastructureError(_FAILED) from None


def _stage(row: Row[tuple[object, ...]]) -> StageRun:
    return StageRun(
        name=row.stage,
        position=row.position,
        status=StageStatus(row.status),
        attempts=row.attempts,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
    )


class SqlRunRepository:
    """Every statement carries the tenant predicate and RLS enforces it again."""

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    @property
    def _tenant(self) -> object:
        return self._tx.tenant_id.value

    def _check(self, tenant_id: TenantId) -> None:
        if tenant_id != self._tx.tenant_id:
            raise InfrastructureError("ingestion_tenant_mismatch")

    def add(self, run: IngestionRun) -> bool:
        self._check(run.tenant_id)
        statement = (
            pg_insert(runs)
            .values(**self._run_values(run, True))
            .on_conflict_do_nothing()  # primary key or (tenant, version, generation)
            .returning(runs.c.run_id)
        )
        if _run(lambda: self._tx.session.execute(statement).first()) is None:
            return False
        for stage in run.stages:
            self._execute(insert(stage_runs).values(**self._stage_values(run, stage)))
        return True

    def _execute(self, statement: Executable) -> None:
        _run(lambda: self._tx.session.execute(statement))

    def get(self, run_id: IngestionRunId, *, for_update: bool = False) -> IngestionRun | None:
        return self._one(runs.c.run_id == run_id.value, for_update=for_update)

    def get_for_version(
        self, version: VersionRef, generation: int, *, for_update: bool = False
    ) -> IngestionRun | None:
        return self._one(
            (runs.c.version_id == version.value) & (runs.c.generation == generation),
            for_update=for_update,
        )

    def latest_for_version(
        self, version: VersionRef, *, for_update: bool = False
    ) -> IngestionRun | None:
        return self._one(
            runs.c.version_id == version.value,
            for_update=for_update,
            newest_first=True,
        )

    def list_for_document(
        self, document: DocumentRef, *, for_update: bool = False
    ) -> tuple[IngestionRun, ...]:
        statement = (
            select(runs)
            .where(runs.c.tenant_id == self._tenant, runs.c.document_id == document.value)
            .order_by(runs.c.version_id, runs.c.generation)
        )
        if for_update:
            statement = statement.with_for_update()
        rows = _run(lambda: self._tx.session.execute(statement).all())
        return tuple(self._build(r) for r in rows)

    def save(self, run: IngestionRun) -> None:
        self._check(run.tenant_id)
        statement = (
            update(runs)
            .where(runs.c.tenant_id == self._tenant, runs.c.run_id == run.id.value)
            .values(**self._run_values(run, False))
            .returning(runs.c.run_id)
        )
        if _run(lambda: self._tx.session.execute(statement).first()) is None:
            raise InfrastructureError("ingestion_run_not_saved")
        for stage in run.stages:
            self._save_stage(run, stage)

    def _save_stage(self, run: IngestionRun, stage: StageRun) -> None:
        values = self._stage_values(run, stage)
        for identity in ("tenant_id", "run_id", "stage", "position"):
            del values[identity]
        statement = (
            update(stage_runs)
            .where(
                stage_runs.c.tenant_id == self._tenant,
                stage_runs.c.run_id == run.id.value,
                stage_runs.c.stage == stage.name,
            )
            .values(**values)
            .returning(stage_runs.c.stage)
        )
        if _run(lambda: self._tx.session.execute(statement).first()) is None:
            raise InfrastructureError("ingestion_run_not_saved")

    def remove_for_document(self, document: DocumentRef) -> int:
        owned = select(runs.c.run_id).where(
            runs.c.tenant_id == self._tenant, runs.c.document_id == document.value
        )
        _run(
            lambda: self._tx.session.execute(
                delete(stage_runs).where(
                    stage_runs.c.tenant_id == self._tenant, stage_runs.c.run_id.in_(owned)
                )
            )
        )
        statement = (
            delete(runs)
            .where(runs.c.tenant_id == self._tenant, runs.c.document_id == document.value)
            .returning(runs.c.run_id)
        )
        return len(_run(lambda: self._tx.session.execute(statement).all()))

    def metrics(self) -> IngestionMetrics:
        session = self._tx.session
        by_status = _run(
            lambda: session.execute(
                select(runs.c.status, func.count())
                .where(runs.c.tenant_id == self._tenant)
                .group_by(runs.c.status)
                .order_by(runs.c.status)
            ).all()
        )
        outcomes = [s.value for s in OUTCOME_STATES]
        unacknowledged = _run(
            lambda: session.execute(
                select(func.count()).where(
                    runs.c.tenant_id == self._tenant,
                    runs.c.status.in_(outcomes),
                    runs.c.acknowledged_at.is_(None),
                )
            ).scalar_one()
        )
        succeeded = stage_runs.c.status == StageStatus.SUCCEEDED.value
        stage_rows = _run(
            lambda: session.execute(
                select(
                    stage_runs.c.stage,
                    func.count().filter(stage_runs.c.attempts >= 1),
                    func.coalesce(func.sum(func.greatest(stage_runs.c.attempts - 1, 0)), 0),
                    func.count().filter(stage_runs.c.status == StageStatus.FAILED.value),
                    func.avg(stage_runs.c.duration_ms).filter(succeeded),
                    func.max(stage_runs.c.duration_ms).filter(succeeded),
                )
                .where(stage_runs.c.tenant_id == self._tenant)
                .group_by(stage_runs.c.stage)
                .order_by(stage_runs.c.stage)
            ).all()
        )
        counts = {status: int(n) for status, n in by_status}
        return IngestionMetrics(
            runs_by_status=tuple(sorted(counts.items())),
            dead_lettered_runs=counts.get(RunStatus.DEAD_LETTERED.value, 0),
            unacknowledged_outcomes=int(unacknowledged),
            stages=tuple(
                StageMetric(
                    stage=name,
                    runs=int(started),
                    retries=int(retries),
                    failures=int(failed),
                    average_duration_ms=None if avg is None else round(avg),
                    maximum_duration_ms=None if peak is None else int(peak),
                )
                for name, started, retries, failed, avg, peak in stage_rows
            ),
        )

    # ---- internals -----------------------------------------------------------------------------

    def _one(
        self, condition: object, *, for_update: bool, newest_first: bool = False
    ) -> IngestionRun | None:
        statement = select(runs).where(runs.c.tenant_id == self._tenant, condition)  # type: ignore[arg-type]
        if newest_first:
            statement = statement.order_by(runs.c.generation.desc()).limit(1)
        if for_update:
            statement = statement.with_for_update()
        row = _run(lambda: self._tx.session.execute(statement).first())
        return None if row is None else self._build(row)

    def _build(self, row: Row[tuple[object, ...]]) -> IngestionRun:
        stage_rows = _run(
            lambda: self._tx.session.execute(
                select(stage_runs)
                .where(stage_runs.c.tenant_id == self._tenant, stage_runs.c.run_id == row.run_id)
                .order_by(stage_runs.c.position)
            ).all()
        )
        return IngestionRun(
            id=IngestionRunId(row.run_id),
            tenant_id=TenantId(row.tenant_id),
            document=DocumentRef(row.document_id),
            version=VersionRef(row.version_id),
            generation=row.generation,
            status=RunStatus(row.status),
            stages=[_stage(r) for r in stage_rows],
            created_at=row.created_at,
            progress_at=row.progress_at,
            finished_at=row.finished_at,
            error_code=row.error_code,
            acknowledged_at=row.acknowledged_at,
            redeliveries=row.redeliveries,
        )

    def _run_values(self, run: IngestionRun, creating: bool) -> dict[str, object]:
        values: dict[str, object] = {
            "status": run.status.value,
            "error_code": run.error_code,
            "progress_at": run.progress_at,
            "finished_at": run.finished_at,
            "acknowledged_at": run.acknowledged_at,
            "redeliveries": run.redeliveries,
        }
        if creating:
            values |= {
                "tenant_id": self._tenant,
                "run_id": run.id.value,
                "document_id": run.document.value,
                "version_id": run.version.value,
                "generation": run.generation,
                "created_at": run.created_at,
            }
        return values

    def _stage_values(self, run: IngestionRun, stage: StageRun) -> dict[str, object]:
        return {
            "tenant_id": self._tenant,
            "run_id": run.id.value,
            "stage": stage.name,
            "position": stage.position,
            "status": stage.status.value,
            "attempts": stage.attempts,
            "started_at": stage.started_at,
            "finished_at": stage.finished_at,
            "duration_ms": stage.duration_ms,
            "error_code": stage.error_code,
        }
