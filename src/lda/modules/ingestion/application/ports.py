"""Ports of the ingestion engine (ADR-010). `PipelineStage` and `ContentScanner` are implemented by
other modules/adapters (the dependency points INTO ingestion); the repository and unit of work are
implemented by this module's infrastructure and testing packages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from lda.modules.ingestion.application.dto.runs import IngestionMetrics
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.modules.ingestion.domain.run import IngestionRun
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.ids import JobId, TenantId
from lda.shared.ports.jobs import JobFence
from lda.shared.ports.unit_of_work import UnitOfWork

# ---- stages ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StageRequest:
    """Identifiers only. `job_id`/`fencing_token` let a stage fence its OWN effect transactions
    with `uow.fence.verify(job_id, fencing_token)` (ADR-012 amendment). Stages must be idempotent:
    a stage that was running when its worker died runs again."""

    tenant_id: TenantId
    document: DocumentRef
    version: VersionRef
    run_id: IngestionRunId
    stage: str
    attempt: int
    job_id: JobId
    fencing_token: int


@dataclass(frozen=True)
class StageSucceeded:
    pass


@dataclass(frozen=True)
class StageRetryable:
    error_code: str


@dataclass(frozen=True)
class StagePermanentFailure:
    error_code: str


@dataclass(frozen=True)
class StageQuarantined:
    error_code: str


StageOutcome = StageSucceeded | StageRetryable | StagePermanentFailure | StageQuarantined


class PipelineStage(Protocol):
    @property
    def name(self) -> str:
        """Identifier `[a-z][a-z0-9_]{0,63}`, unique in the plan, never `scan`."""
        ...

    def run(self, request: StageRequest) -> StageOutcome: ...


# ---- scanner (ADR-036) -------------------------------------------------------------------------


class ScanVerdict(StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    ERROR = "error"


@dataclass(frozen=True)
class ScanRequest:
    tenant_id: TenantId
    document: DocumentRef
    version: VersionRef
    run_id: IngestionRunId


class ContentScanner(Protocol):
    def scan(self, request: ScanRequest) -> ScanVerdict: ...


# ---- persistence -------------------------------------------------------------------------------


class RunRepository(Protocol):
    """Bound to one tenant for its whole life. `for_update` takes the row lock."""

    def add(self, run: IngestionRun) -> bool:
        """Insert-if-absent: False when a run with this id (or version and generation) exists,
        e.g. a concurrent executor created it first. Nothing is written in that case."""
        ...

    def get(self, run_id: IngestionRunId, *, for_update: bool = False) -> IngestionRun | None: ...

    def get_for_version(
        self, version: VersionRef, generation: int, *, for_update: bool = False
    ) -> IngestionRun | None: ...

    def latest_for_version(
        self, version: VersionRef, *, for_update: bool = False
    ) -> IngestionRun | None:
        """The run with the highest generation."""
        ...

    def list_for_document(
        self, document: DocumentRef, *, for_update: bool = False
    ) -> tuple[IngestionRun, ...]: ...

    def save(self, run: IngestionRun) -> None:
        """Persist the mutable state of the run and its stages (identity never changes)."""
        ...

    def remove_for_document(self, document: DocumentRef) -> int: ...

    def metrics(self) -> IngestionMetrics: ...


class IngestionUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> RunRepository: ...

    @property
    def fence(self) -> JobFence: ...


class IngestionUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> IngestionUnitOfWork: ...
