from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.shared.kernel.ids import JobId


@dataclass(frozen=True)
class ExecuteRunCommand:
    """What one claimed `ingestion:run` job asks for. Ids and counters only; the tenant is not
    here: it comes from the execution context (the job row)."""

    document: DocumentRef
    version: VersionRef
    generation: int
    job_id: JobId
    fencing_token: int
    attempt: int
    max_attempts: int


class ExecutionOutcome(StrEnum):
    FINISHED = "finished"  # a terminal outcome was recorded and published
    INACTIVE = "inactive"  # nothing to do: dead-lettered, cancelled, finished or stale
    RETRY = "retry"  # a retryable failure was recorded; ask the worker for another attempt
    DEAD_LETTERED = "dead_lettered"  # retries exhausted; the run is dead-lettered


@dataclass(frozen=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    error_code: str | None = None


@dataclass(frozen=True)
class WatchdogCommand:
    run_id: IngestionRunId
    job_id: JobId
    fencing_token: int


@dataclass(frozen=True)
class WatchdogResult:
    done: bool
    next_check_seconds: int = 0


@dataclass(frozen=True)
class RunReference:
    run_id: IngestionRunId


@dataclass(frozen=True)
class DocumentReferenceCommand:
    document: DocumentRef


@dataclass(frozen=True)
class VersionQuery:
    version: VersionRef


@dataclass(frozen=True)
class ReplayCommand:
    version: VersionRef


@dataclass(frozen=True)
class ReplayView:
    run_id: IngestionRunId
    generation: int


@dataclass(frozen=True)
class StageView:
    name: str
    status: str
    attempts: int
    duration_ms: int | None
    error_code: str | None


@dataclass(frozen=True)
class RunView:
    run_id: IngestionRunId
    document: DocumentRef
    version: VersionRef
    generation: int
    status: str
    error_code: str | None
    acknowledged: bool
    redeliveries: int
    stages: tuple[StageView, ...]


@dataclass(frozen=True)
class StageMetric:
    stage: str
    runs: int  # stage runs that have started at least once
    retries: int  # attempts beyond the first, summed
    failures: int  # stage runs that ended `failed`
    average_duration_ms: int | None  # over succeeded stage runs
    maximum_duration_ms: int | None


@dataclass(frozen=True)
class IngestionMetrics:
    """Per tenant. Queue depth/oldest age/dead jobs come from the platform's `JobMaintenance`."""

    runs_by_status: tuple[tuple[str, int], ...]
    dead_lettered_runs: int
    unacknowledged_outcomes: int
    stages: tuple[StageMetric, ...]
