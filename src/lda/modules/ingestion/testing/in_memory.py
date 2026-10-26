"""In-memory ingestion adapters with the same observable behaviour as the SQL ones.

A unit of work works on copies and publishes them to the shared store only on `commit`, so rollback,
tenant isolation, locking-free state machines and the fence can be tested without a database. A
contract suite runs against both adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from types import TracebackType
from typing import Self
from uuid import UUID

from lda.modules.ingestion.application.dto.runs import IngestionMetrics, StageMetric
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.modules.ingestion.domain.run import (
    OUTCOME_STATES,
    IngestionRun,
    RunStatus,
    StageRun,
    StageStatus,
)
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.events import DomainEvent
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.audit import AuditFact
from lda.shared.ports.events import Delivery
from lda.shared.ports.jobs import JobSpec

RunKey = tuple[TenantId, IngestionRunId]


def copy_run(run: IngestionRun) -> IngestionRun:
    return replace(run, stages=[replace(s) for s in run.stages])


class InMemoryIngestionStore:
    """The committed state, shared by every unit of work of one test."""

    def __init__(self) -> None:
        self.runs: dict[RunKey, IngestionRun] = {}
        self.unique_keys: set[tuple[TenantId, str]] = set()
        self.committed_jobs: list[tuple[TenantId, JobSpec]] = []
        self.committed_events: list[tuple[DomainEvent, Delivery]] = []
        self.committed_facts: list[AuditFact] = []
        # Tests make the fence fail like a lost lease: return True to reject (job, token).
        self.fence_rejects: Callable[[JobId, int], bool] | None = None
        self.fail_commit_on: set[int] = set()  # 0-based index of units of work whose commit fails


class _Recorder:
    def __init__(self, tenant_id: TenantId, store: InMemoryIngestionStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self.facts: list[AuditFact] = []
        self.events: list[tuple[DomainEvent, Delivery]] = []
        self.jobs: list[JobSpec] = []

    def record(self, fact: AuditFact) -> None:
        self.facts.append(fact)

    def publish(self, event: DomainEvent, delivery: Delivery) -> None:
        if event.tenant_id != self._tenant_id:
            raise InfrastructureError("event_tenant_mismatch")
        self.events.append((event, delivery))

    def enqueue(self, spec: JobSpec) -> JobId | None:
        key = None if spec.unique_key is None else (self._tenant_id, spec.unique_key)
        taken = key in self._store.unique_keys or any(
            s.unique_key == spec.unique_key and spec.unique_key is not None for s in self.jobs
        )
        if key is not None and taken:
            return None
        self.jobs.append(spec)
        return JobId(UUID(int=0xE000 + len(self._store.committed_jobs) + len(self.jobs)))


class _Fence:
    def __init__(self, store: InMemoryIngestionStore) -> None:
        self._store = store

    def verify(self, job_id: JobId, fencing_token: int) -> None:
        rejects = self._store.fence_rejects
        if rejects is not None and rejects(job_id, fencing_token):
            raise ConflictError("job_lease_lost")


class InMemoryRunRepository:
    def __init__(self, tenant_id: TenantId, working: dict[RunKey, IngestionRun]) -> None:
        self._tenant_id = tenant_id
        self._working = working

    def _mine(self) -> list[IngestionRun]:
        return [r for (t, _), r in self._working.items() if t == self._tenant_id]

    def add(self, run: IngestionRun) -> bool:
        if run.tenant_id != self._tenant_id:
            raise InfrastructureError("ingestion_tenant_mismatch")
        duplicate = any(
            r.version == run.version and r.generation == run.generation for r in self._mine()
        )
        if (run.tenant_id, run.id) in self._working or duplicate:
            return False
        self._working[(run.tenant_id, run.id)] = copy_run(run)
        return True

    def get(self, run_id: IngestionRunId, *, for_update: bool = False) -> IngestionRun | None:
        found = self._working.get((self._tenant_id, run_id))
        return None if found is None else copy_run(found)

    def get_for_version(
        self, version: VersionRef, generation: int, *, for_update: bool = False
    ) -> IngestionRun | None:
        for run in self._mine():
            if run.version == version and run.generation == generation:
                return copy_run(run)
        return None

    def latest_for_version(
        self, version: VersionRef, *, for_update: bool = False
    ) -> IngestionRun | None:
        mine = [r for r in self._mine() if r.version == version]
        return copy_run(max(mine, key=lambda r: r.generation)) if mine else None

    def list_for_document(
        self, document: DocumentRef, *, for_update: bool = False
    ) -> tuple[IngestionRun, ...]:
        found = [r for r in self._mine() if r.document == document]
        found.sort(key=lambda r: (str(r.version), r.generation))
        return tuple(copy_run(r) for r in found)

    def save(self, run: IngestionRun) -> None:
        if run.tenant_id != self._tenant_id:
            raise InfrastructureError("ingestion_tenant_mismatch")
        if (run.tenant_id, run.id) not in self._working:
            raise InfrastructureError("ingestion_run_not_saved")
        self._working[(run.tenant_id, run.id)] = copy_run(run)

    def remove_for_document(self, document: DocumentRef) -> int:
        doomed = [
            k
            for k, r in self._working.items()
            if k[0] == self._tenant_id and r.document == document
        ]
        for key in doomed:
            del self._working[key]
        return len(doomed)

    def metrics(self) -> IngestionMetrics:
        runs = self._mine()
        counts: dict[str, int] = {}
        for run in runs:
            counts[run.status.value] = counts.get(run.status.value, 0) + 1
        by_stage: dict[str, list[StageRun]] = {}
        for run in runs:
            for stage in run.stages:
                by_stage.setdefault(stage.name, []).append(stage)
        stages = []
        for name in sorted(by_stage):
            rows = by_stage[name]
            durations = [
                s.duration_ms
                for s in rows
                if s.status is StageStatus.SUCCEEDED and s.duration_ms is not None
            ]
            stages.append(
                StageMetric(
                    stage=name,
                    runs=sum(1 for s in rows if s.attempts >= 1),
                    retries=sum(max(s.attempts - 1, 0) for s in rows),
                    failures=sum(1 for s in rows if s.status is StageStatus.FAILED),
                    average_duration_ms=round(sum(durations) / len(durations))
                    if durations
                    else None,
                    maximum_duration_ms=max(durations) if durations else None,
                )
            )
        return IngestionMetrics(
            runs_by_status=tuple(sorted(counts.items())),
            dead_lettered_runs=counts.get(RunStatus.DEAD_LETTERED.value, 0),
            unacknowledged_outcomes=sum(
                1 for r in runs if r.status in OUTCOME_STATES and r.acknowledged_at is None
            ),
            stages=tuple(stages),
        )


class InMemoryIngestionUnitOfWork:
    def __init__(self, tenant_id: TenantId, store: InMemoryIngestionStore, index: int) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._index = index
        self._working = {k: copy_run(v) for k, v in store.runs.items()}
        self._shared = _Recorder(tenant_id, store)
        self._runs = InMemoryRunRepository(tenant_id, self._working)
        self._fence = _Fence(store)
        self.committed = False
        self.rolled_back = False

    @property
    def tenant_id(self) -> TenantId:
        return self._tenant_id

    @property
    def events(self) -> _Recorder:
        return self._shared

    @property
    def jobs(self) -> _Recorder:
        return self._shared

    @property
    def audit(self) -> _Recorder:
        return self._shared

    @property
    def fence(self) -> _Fence:
        return self._fence

    @property
    def runs(self) -> InMemoryRunRepository:
        return self._runs

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self.committed:
            self.rolled_back = True

    def commit(self) -> None:
        if self._index in self._store.fail_commit_on:
            raise InfrastructureError("database_unavailable")
        self._store.runs = self._working
        for spec in self._shared.jobs:
            if spec.unique_key is not None:
                self._store.unique_keys.add((self._tenant_id, spec.unique_key))
            self._store.committed_jobs.append((self._tenant_id, spec))
        self._store.committed_events.extend(self._shared.events)
        self._store.committed_facts.extend(self._shared.facts)
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


class InMemoryIngestionUnitOfWorkFactory:
    def __init__(self, store: InMemoryIngestionStore | None = None) -> None:
        self.store = store or InMemoryIngestionStore()
        self.begun: list[InMemoryIngestionUnitOfWork] = []

    def begin(self, ctx: ExecutionContext) -> InMemoryIngestionUnitOfWork:
        uow = InMemoryIngestionUnitOfWork(ctx.tenant_id, self.store, len(self.begun))
        self.begun.append(uow)
        return uow
