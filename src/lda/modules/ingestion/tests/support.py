from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from lda.modules.ingestion.application.dto.runs import ExecuteRunCommand
from lda.modules.ingestion.application.pipeline import Pipeline
from lda.modules.ingestion.application.ports import PipelineStage
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
from lda.modules.ingestion.domain.ids import DocumentRef, VersionRef
from lda.modules.ingestion.testing import (
    InMemoryIngestionUnitOfWorkFactory,
    ScriptedScanner,
    ScriptedStage,
)
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.ids import JobId, PrincipalId, RequestId, TenantId

TENANT_A = TenantId(UUID(int=0xA1))
TENANT_B = TenantId(UUID(int=0xB2))
START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
D1, D2 = DocumentRef(UUID(int=0xD1)), DocumentRef(UUID(int=0xD2))
V1, V2 = VersionRef(UUID(int=0x51)), VersionRef(UUID(int=0x52))
DELAY = timedelta(seconds=1800)
MAX_REDELIVERIES = 2
SYSTEM_A = SystemContext(TENANT_A, "ingestion-test")
SYSTEM_B = SystemContext(TENANT_B, "ingestion-test")
ALICE = PrincipalId(UUID(int=0xA11CE))


def user(tenant: TenantId = TENANT_A) -> TenantContext:
    return TenantContext(tenant, ALICE, RequestId(UUID(int=7)))


def command(
    *,
    version: VersionRef = V1,
    document: DocumentRef = D1,
    generation: int = 1,
    job: int = 1,
    token: int = 1,
    attempt: int = 1,
    max_attempts: int = 3,
) -> ExecuteRunCommand:
    return ExecuteRunCommand(
        document, version, generation, JobId(UUID(int=job)), token, attempt, max_attempts
    )


@dataclass
class Rig:
    factory: InMemoryIngestionUnitOfWorkFactory
    clock: FakeClock
    ids: SequentialIdGenerator
    scanner: ScriptedScanner
    stages: Sequence[ScriptedStage]
    pipeline: Pipeline
    starter: RunStarter
    execute: ExecuteRun
    watchdog: RunWatchdog
    replay: ReplayIngestion
    acknowledge: AcknowledgeOutcome
    cancel: CancelIngestion
    purge: PurgeIngestionRecords
    status: GetIngestionStatus
    metrics: GetIngestionMetrics


def build_rig(
    factory: InMemoryIngestionUnitOfWorkFactory,
    clock: FakeClock,
    ids: SequentialIdGenerator,
    stages: Sequence[PipelineStage] | None = None,
    scanner: ScriptedScanner | None = None,
) -> Rig:
    chosen = (
        list(stages)
        if stages is not None
        else [ScriptedStage("fake_one"), ScriptedStage("fake_two")]
    )
    scan = scanner or ScriptedScanner()
    pipeline = Pipeline(scan, chosen)
    starter = RunStarter(pipeline, DELAY)
    return Rig(
        factory=factory,
        clock=clock,
        ids=ids,
        scanner=scan,
        stages=chosen,  # type: ignore[arg-type]
        pipeline=pipeline,
        starter=starter,
        execute=ExecuteRun(factory, pipeline, starter, clock, ids),
        watchdog=RunWatchdog(factory, clock, ids, DELAY, MAX_REDELIVERIES),
        replay=ReplayIngestion(factory, starter, clock),
        acknowledge=AcknowledgeOutcome(factory, clock),
        cancel=CancelIngestion(factory, clock),
        purge=PurgeIngestionRecords(factory),
        status=GetIngestionStatus(factory),
        metrics=GetIngestionMetrics(factory),
    )
