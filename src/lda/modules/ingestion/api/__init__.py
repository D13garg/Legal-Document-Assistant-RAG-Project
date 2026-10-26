"""Public surface of ingestion (the engine: runs, stage runs, recovery; ADR-010).

Other modules implement `PipelineStage`/`ContentScanner` and consume the outcome events and the
system use cases; they never see ingestion's persistence."""

from lda.modules.ingestion.application.dto.runs import (
    DocumentReferenceCommand,
    ExecuteRunCommand,
    ExecutionOutcome,
    ExecutionResult,
    IngestionMetrics,
    ReplayCommand,
    ReplayView,
    RunReference,
    RunView,
    StageMetric,
    StageView,
    VersionQuery,
    WatchdogCommand,
    WatchdogResult,
)
from lda.modules.ingestion.application.errors import RetryRequested
from lda.modules.ingestion.application.job_handlers import RunJobHandler, WatchdogJobHandler
from lda.modules.ingestion.application.jobs import (
    INGESTION_QUEUE,
    RUN_JOB,
    WATCHDOG_JOB,
    ingestion_job_spec,
)
from lda.modules.ingestion.application.pipeline import SCAN_STAGE, Pipeline
from lda.modules.ingestion.application.ports import (
    ContentScanner,
    PipelineStage,
    ScanRequest,
    ScanVerdict,
    StageOutcome,
    StagePermanentFailure,
    StageQuarantined,
    StageRequest,
    StageRetryable,
    StageSucceeded,
)
from lda.modules.ingestion.application.queries.get_ingestion_metrics import GetIngestionMetrics
from lda.modules.ingestion.application.queries.get_ingestion_status import GetIngestionStatus
from lda.modules.ingestion.application.starter import RunStarter
from lda.modules.ingestion.application.use_cases.acknowledge_outcome import AcknowledgeOutcome
from lda.modules.ingestion.application.use_cases.cancel_ingestion import CancelIngestion
from lda.modules.ingestion.application.use_cases.execute_run import ExecuteRun
from lda.modules.ingestion.application.use_cases.purge_ingestion_records import (
    PurgeIngestionRecords,
)
from lda.modules.ingestion.application.use_cases.replay_ingestion import ReplayIngestion
from lda.modules.ingestion.application.use_cases.run_watchdog import RunWatchdog
from lda.modules.ingestion.domain.events import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.modules.ingestion.domain.run import RunStatus, StageStatus

__all__ = [
    "INGESTION_QUEUE",
    "RUN_JOB",
    "SCAN_STAGE",
    "WATCHDOG_JOB",
    "AcknowledgeOutcome",
    "CancelIngestion",
    "ContentScanner",
    "DocumentRef",
    "DocumentReferenceCommand",
    "ExecuteRun",
    "ExecuteRunCommand",
    "ExecutionOutcome",
    "ExecutionResult",
    "GetIngestionMetrics",
    "GetIngestionStatus",
    "IngestionCompleted",
    "IngestionFailed",
    "IngestionMetrics",
    "IngestionQuarantined",
    "IngestionRunId",
    "Pipeline",
    "PipelineStage",
    "PurgeIngestionRecords",
    "ReplayCommand",
    "ReplayIngestion",
    "ReplayView",
    "RetryRequested",
    "RunJobHandler",
    "RunReference",
    "RunStarter",
    "RunStatus",
    "RunView",
    "RunWatchdog",
    "ScanRequest",
    "ScanVerdict",
    "StageMetric",
    "StageOutcome",
    "StagePermanentFailure",
    "StageQuarantined",
    "StageRequest",
    "StageRetryable",
    "StageStatus",
    "StageSucceeded",
    "StageView",
    "VersionQuery",
    "VersionRef",
    "WatchdogCommand",
    "WatchdogJobHandler",
    "WatchdogResult",
    "ingestion_job_spec",
]
