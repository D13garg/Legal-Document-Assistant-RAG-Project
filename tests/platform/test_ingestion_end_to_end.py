"""Step 6 end to end: upload -> event job -> ingestion job -> outcome event -> Documents status,
through the REAL Postgres queue, the REAL worker, the REAL unit of work, with one FakeClock.

Every scenario drives `JobWorker.run_once`; nothing calls a use case on the worker's behalf except
where the test plays the part of a person (upload, delete, replay) or of a crashed process.
"""

from __future__ import annotations

import base64
import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.access_control import (
    ACCESS_CONTROL_MIGRATIONS,
    build_access_control,
    load_access_control_settings,
)
from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.documents import (
    DOCUMENTS_MIGRATIONS,
    DocsContainer,
    build_docs,
    load_docs_settings,
    register_doc_resources,
)
from lda.bootstrap.identity import IDENTITY_MIGRATIONS, build_identity
from lda.bootstrap.ingestion import (
    INGESTION_MIGRATIONS,
    IngestionContainer,
    build_ingestion,
    load_ingestion_settings,
)
from lda.modules.access_control.api import ResourceRegistry
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.documents.api import DocumentId, DocumentReference, UploadDocumentCommand
from lda.modules.identity.api import PrincipalKind, RegisterPrincipalCommand
from lda.modules.ingestion.api import (
    PipelineStage,
    ReplayCommand,
    ScanVerdict,
    StagePermanentFailure,
    StageRetryable,
    VersionQuery,
    VersionRef,
)
from lda.modules.ingestion.testing import ScriptedScanner, ScriptedStage
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.jobs.leases import JobLeaseStore
from lda.platform.database.jobs.maintenance import JobMaintenance
from lda.platform.database.jobs.worker import BackoffPolicy, JobWorker
from lda.platform.database.unit_of_work import SqlUnitOfWorkFactory
from lda.platform.events.codec import EventCodec
from lda.platform.events.publisher import TransactionalEventPublisher
from lda.platform.events.registry import HandlerRegistry
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore
from lda.shared.kernel.context import SystemContext, TenantContext
from lda.shared.kernel.ids import RequestId, TenantId
from platform_support import Database, provisioned_database

KEY = base64.urlsafe_b64encode(b"ingestion-e2e-test-key-32-bytes!").decode().rstrip("=")
T1, T2 = TenantId(UUID(int=0x71)), TenantId(UUID(int=0x72))
START = datetime(2026, 1, 1, tzinfo=UTC)
PDF, BODY = "application/pdf", b"%PDF-1.7 TOP-SECRET clause"


class Crash(BaseException):
    """A process dying: not an `Exception`, so nothing in the engine may swallow it."""


class Stack:
    def __init__(
        self,
        db: Database,
        scanner: ScriptedScanner | None = None,
        stages: list[PipelineStage] | None = None,
    ) -> None:
        env = {
            "LDA_DATABASE__URL": db.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1, "documents": 1, "ingestion": 1}',
            "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
            "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        }
        config = ConfigurationLoader(env)
        base = build_platform(config, audit_factory=SqlAuditRecorder)
        self.engine = create_engine(db.app_url)
        self.clock = FakeClock(START)
        handlers, codec = HandlerRegistry(), EventCodec()
        units = SqlUnitOfWorkFactory(
            self.engine,
            self.clock,
            base.ids,
            SqlAuditRecorder,
            lambda uow, jobs: TransactionalEventPublisher(handlers, codec, jobs, uow),
        )
        self.platform: PlatformContainer = dataclasses.replace(
            base,
            clock=self.clock,
            units_of_work=units,
            handlers=handlers,
            event_codec=codec,
            job_leases=JobLeaseStore(self.engine, self.clock),
            job_maintenance=JobMaintenance(self.engine, self.clock),
            object_store=InMemoryObjectStore(),
        )
        self._base = base
        identity = build_identity(self.platform)
        registry = ResourceRegistry()
        register_doc_resources(registry)
        access = build_access_control(
            self.platform, identity, load_access_control_settings(config), registry
        )
        self.scanner = scanner or ScriptedScanner()
        self.stages: list[PipelineStage] = stages or [ScriptedStage("fake_one")]
        self.ingestion: IngestionContainer = build_ingestion(
            self.platform, load_ingestion_settings(config), scanner=self.scanner, stages=self.stages
        )
        self.docs: DocsContainer = build_docs(
            self.platform, access, self.ingestion, load_docs_settings(config)
        )
        self.identity = identity
        admin = SystemContext(T1, "ingestion-e2e")
        self.alice = identity.register_principal.execute(
            admin, RegisterPrincipalCommand(PrincipalKind.USER, "Alice")
        ).principal_id
        self.handlers = {**self.docs.job_handlers, **self.ingestion.job_handlers}
        self.worker = self.make_worker("worker-1")

    def make_worker(self, name: str, queues: dict[str, int] | None = None) -> JobWorker:
        settings = self.platform.job_settings
        if queues is not None:
            settings = settings.model_copy(update={"queues": queues})
        return JobWorker(
            self.platform.job_leases,
            self.handlers,
            settings,
            self.clock,
            name,
            BackoffPolicy(1.0, 10.0, jitter=lambda: 0.0),
            self.platform.job_maintenance,
        )

    def ctx(self, tenant: TenantId = T1) -> TenantContext:
        return TenantContext(tenant, self.alice, RequestId(UUID(int=9)))

    def upload(self, tenant: TenantId = T1) -> tuple[UUID, UUID]:
        view = self.docs.upload_document.execute(
            self.ctx(tenant), UploadDocumentCommand("Privileged merger memo", PDF, BODY)
        )
        return view.document_id.value, view.version_id.value

    def close(self) -> None:
        self._base.dispose()
        self.engine.dispose()


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_ingestion_e2e",
        "lda_app_ingestion_e2e",
        [
            IDENTITY_MIGRATIONS,
            ACCESS_CONTROL_MIGRATIONS,
            AUDIT_MIGRATIONS,
            DOCUMENTS_MIGRATIONS,
            INGESTION_MIGRATIONS,
        ],
    ) as db:
        yield db


@pytest.fixture
def oracle(database: Database) -> Iterator[Engine]:
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        for table in (
            "audit.records",
            "documents.purge_receipts",
            "documents.upload_intents",
            "documents.versions",
            "documents.documents",
            "access_control.grants",
            "access_control.policy_epochs",
            "identity.principals",
            "ingestion.stage_runs",
            "ingestion.runs",
            "platform.jobs",
        ):
            conn.execute(text(f"DELETE FROM {table}"))  # noqa: S608
    yield engine
    engine.dispose()


@pytest.fixture
def stack(database: Database, oracle: Engine) -> Iterator[Stack]:
    s = Stack(database)
    yield s
    s.close()


def scalar(oracle: Engine, sql: str, **params: object) -> object:
    with oracle.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def version_status(oracle: Engine, version_id: UUID) -> str:
    return str(
        scalar(
            oracle, "SELECT status FROM documents.versions WHERE version_id = :v", v=str(version_id)
        )
    )


def drain(s: Stack, oracle: Engine, rounds: int = 80, follow_retries: bool = True) -> int:
    """Run the worker until nothing is runnable. Delayed RETRIES are followed by moving the clock;
    watchdog jobs are not (tests move the clock for those on purpose)."""
    total = 0
    for _ in range(rounds):
        ran = s.worker.run_once()
        total += ran
        if ran:
            continue
        if not follow_retries:
            break
        nxt = scalar(
            oracle,
            "SELECT min(run_at) FROM platform.jobs WHERE status = 'queued' AND run_at > :now "
            "AND job_type <> 'ingestion:watchdog'",
            now=s.clock.now(),
        )
        if nxt is None:
            break
        s.clock.advance(nxt - s.clock.now() + timedelta(milliseconds=1))  # type: ignore[operator]
    return total


def fetch(oracle: Engine, sql: str, **params: object) -> list[tuple[object, ...]]:
    with oracle.connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).all()]


def job_statuses(oracle: Engine) -> dict[str, int]:
    with oracle.connect() as conn:
        rows = conn.execute(
            text("SELECT status, count(*) FROM platform.jobs GROUP BY status")
        ).all()
    return {r[0]: r[1] for r in rows}


def audit_actions(oracle: Engine) -> list[str]:
    with oracle.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT action FROM audit.records ORDER BY occurred_at, record_id")
            )
        ]


# ---- the happy path ----------------------------------------------------------------------------


def test_a_new_version_is_pending_until_every_stage_ran_and_then_becomes_ready(
    stack: Stack, oracle: Engine
) -> None:
    _, version_id = stack.upload()
    assert version_status(oracle, version_id) == "pending"  # never searchable before ingestion
    assert drain(stack, oracle) >= 3  # event job, ingestion job, outcome job (+ ack)
    assert version_status(oracle, version_id) == "ready"
    only: ScriptedStage = stack.stages[0]  # type: ignore[assignment]
    assert len(only.calls) == 1 and len(stack.scanner.requests) == 1
    ((state, acked),) = fetch(oracle, "SELECT status, acknowledged_at FROM ingestion.runs")
    assert state == "completed" and acked is not None  # Documents acknowledged the outcome
    statuses = {r[0] for r in fetch(oracle, "SELECT status FROM ingestion.stage_runs")}
    assert statuses == {"succeeded"}
    assert set(job_statuses(oracle)) <= {
        "succeeded",
        "queued",
    }  # nothing dead; only the watchdog waits
    assert "documents.version_outcome_recorded" in audit_actions(oracle)
    assert audit_actions(oracle).count("documents.version_outcome_recorded") == 1


def test_the_outcome_job_runs_only_after_the_ingestion_run_is_complete(
    stack: Stack, oracle: Engine
) -> None:
    """Drive the queue one job at a time and watch the version: it can only flip at the very end."""
    _, version_id = stack.upload()
    seen: list[str] = []
    for _ in range(12):
        if stack.worker.run_once() == 0:
            break
        seen.append(version_status(oracle, version_id))
    assert seen[-1] == "ready" and seen.count("ready") >= 1
    assert seen[0] == "pending"
    run_status = scalar(oracle, "SELECT status FROM ingestion.runs")
    assert run_status == "completed"


# ---- malware, permanent failure ----------------------------------------------------------------


def test_malware_quarantines_the_version_and_no_stage_after_the_scan_runs(
    database: Database, oracle: Engine
) -> None:
    after = ScriptedStage("fake_one")
    s = Stack(database, ScriptedScanner([ScanVerdict.INFECTED]), [after])
    try:
        _, version_id = s.upload()
        drain(s, oracle)
        assert version_status(oracle, version_id) == "quarantined"
        assert after.calls == []
        assert scalar(oracle, "SELECT status FROM ingestion.runs") == "quarantined"
        # terminal: nothing can move it any more, not even a direct attempt at the database
        for target in ("ready", "failed", "pending"):
            with pytest.raises(Exception, match="terminal"), oracle.begin() as conn:
                conn.execute(text("SET LOCAL session_replication_role = origin"))
                conn.execute(text("UPDATE documents.versions SET status = :s"), {"s": target})
    finally:
        s.close()


def test_a_permanent_stage_failure_fails_the_version(database: Database, oracle: Engine) -> None:
    s = Stack(
        database, None, [ScriptedStage("fake_one", [StagePermanentFailure("unsupported_format")])]
    )
    try:
        _, version_id = s.upload()
        drain(s, oracle)
        assert version_status(oracle, version_id) == "failed"
        assert scalar(oracle, "SELECT error_code FROM ingestion.runs") == "unsupported_format"
    finally:
        s.close()


# ---- retry, dead letter, replay ----------------------------------------------------------------


def test_transient_failures_are_retried_with_backoff_and_then_succeed(
    database: Database, oracle: Engine
) -> None:
    flaky = ScriptedStage("fake_one", [StageRetryable("busy"), StageRetryable("busy")])
    s = Stack(database, None, [flaky])
    try:
        _, version_id = s.upload()
        drain(s, oracle)
        assert version_status(oracle, version_id) == "ready"
        assert len(flaky.calls) == 3
        assert (
            scalar(oracle, "SELECT attempts FROM ingestion.stage_runs WHERE stage = 'fake_one'")
            == 3
        )
        assert (
            scalar(oracle, "SELECT attempts FROM platform.jobs WHERE job_type = 'ingestion:run'")
            == 3
        )
        assert len(s.scanner.requests) == 1  # finished stages are not repeated by a retry
    finally:
        s.close()


def test_exhausted_retries_dead_letter_the_run_and_leave_the_version_pending_until_replay(
    database: Database, oracle: Engine
) -> None:
    always = ScriptedStage("fake_one", [StageRetryable("busy")] * 5)
    s = Stack(database, None, [always])
    try:
        _, version_id = s.upload()
        drain(s, oracle)
        # the queue's retry budget (max_attempts = 5) is exhausted: the JOB is dead, the RUN is
        # dead-lettered, and the VERSION is untouched
        assert (
            scalar(oracle, "SELECT status FROM platform.jobs WHERE job_type = 'ingestion:run'")
            == "dead"
        )
        assert scalar(oracle, "SELECT status FROM ingestion.runs") == "dead_lettered"
        assert version_status(oracle, version_id) == "pending"
        assert len(always.calls) == 5

        view = s.ingestion.replay_ingestion.execute(s.ctx(), ReplayCommand(VersionRef(version_id)))
        assert view.generation == 2
        drain(s, oracle)
        assert version_status(oracle, version_id) == "ready"
        generations = fetch(
            oracle, "SELECT generation, status FROM ingestion.runs ORDER BY generation"
        )
        assert generations == [(1, "dead_lettered"), (2, "completed")]
        assert "ingestion.ingestion_replayed" in audit_actions(oracle)
        status = s.ingestion.get_ingestion_status.execute(
            s.ctx(), VersionQuery(VersionRef(version_id))
        )
        assert status is not None and (status.generation, status.status) == (2, "completed")
        metrics = s.ingestion.get_ingestion_metrics.execute(s.ctx())
        assert dict(metrics.runs_by_status) == {"completed": 1, "dead_lettered": 1}
        assert metrics.dead_lettered_runs == 1
    finally:
        s.close()


def test_replay_cannot_resurrect_a_failed_version(database: Database, oracle: Engine) -> None:
    from lda.shared.kernel.errors import ConflictError

    s = Stack(database, None, [ScriptedStage("fake_one", [StagePermanentFailure("bad")])])
    try:
        _, version_id = s.upload()
        drain(s, oracle)
        with pytest.raises(ConflictError, match="ingestion_replay_not_allowed"):
            s.ingestion.replay_ingestion.execute(s.ctx(), ReplayCommand(VersionRef(version_id)))
        assert version_status(oracle, version_id) == "failed"
    finally:
        s.close()


# ---- duplicate delivery ------------------------------------------------------------------------


def test_replaying_every_succeeded_job_changes_nothing(stack: Stack, oracle: Engine) -> None:
    _, version_id = stack.upload()
    drain(stack, oracle)
    before = (
        version_status(oracle, version_id),
        audit_actions(oracle),
        scalar(oracle, "SELECT count(*) FROM platform.jobs"),
    )
    with oracle.begin() as conn:  # at-least-once: the queue hands EVERY job out a second time
        conn.execute(
            text(
                "UPDATE platform.jobs SET status = 'queued', attempts = 0, finished_at = NULL, "
                "run_at = :now WHERE status = 'succeeded'"
            ),
            {"now": stack.clock.now()},
        )
    drain(stack, oracle)
    after = (
        version_status(oracle, version_id),
        audit_actions(oracle),
        scalar(oracle, "SELECT count(*) FROM platform.jobs"),
    )
    assert after == before  # no second audit fact, no new job, no status change
    only: ScriptedStage = stack.stages[0]  # type: ignore[assignment]
    assert len(only.calls) == 1  # the stage never ran twice


# ---- deletion while the pipeline runs ----------------------------------------------------------


def test_deleting_a_pending_document_cancels_ingestion_and_never_makes_it_ready(
    stack: Stack, oracle: Engine
) -> None:
    document_id, version_id = stack.upload()
    stack.worker.run_once()  # events queue: the start-pipeline job (ingestion job now queued)
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document_id)))
    # serve everything EXCEPT the documents queue, so the purge has not run yet
    stack.worker = stack.make_worker("no-purge", {"events": 1, "ingestion": 1})
    drain(stack, oracle)
    assert scalar(oracle, "SELECT status FROM documents.documents") == "deleted"
    assert version_status(oracle, version_id) == "pending"  # not ready, not failed: untouched
    assert "documents.version_outcome_recorded" not in audit_actions(oracle)
    assert scalar(oracle, "SELECT status FROM ingestion.runs") in {"cancelled", "completed"}
    # now the purge runs: everything of the document disappears, ingestion state included
    stack.worker = stack.make_worker("all")
    drain(stack, oracle)
    assert scalar(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert scalar(oracle, "SELECT count(*) FROM ingestion.runs") == 0
    assert "documents.version_outcome_recorded" not in audit_actions(oracle)


def test_purge_removes_the_ingestion_state_of_the_document(stack: Stack, oracle: Engine) -> None:
    document_id, _ = stack.upload()
    drain(stack, oracle)
    assert scalar(oracle, "SELECT count(*) FROM ingestion.runs") == 1
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document_id)))
    drain(stack, oracle)
    assert scalar(oracle, "SELECT count(*) FROM documents.documents") == 0  # purged
    assert scalar(oracle, "SELECT count(*) FROM ingestion.runs") == 0
    assert scalar(oracle, "SELECT count(*) FROM ingestion.stage_runs") == 0
    receipts = scalar(oracle, "SELECT count(*) FROM documents.purge_receipts")
    assert receipts == 1  # Documents' own receipt only; ingestion wrote none


# ---- recovery ----------------------------------------------------------------------------------


def test_a_lost_outcome_event_is_republished_by_the_watchdog(stack: Stack, oracle: Engine) -> None:
    _, version_id = stack.upload()
    # run the pipeline, but "lose" the outcome delivery: kill the record-ready job before it runs
    for _ in range(6):
        stack.worker.run_once()
        if scalar(oracle, "SELECT count(*) FROM ingestion.runs WHERE status = 'completed'"):
            break
    with oracle.begin() as conn:
        conn.execute(
            text(
                "UPDATE platform.jobs SET status = 'dead' WHERE job_type = 'event:documents.record_ready'"
            )
        )
    drain(stack, oracle)
    assert version_status(oracle, version_id) == "pending"  # the outcome never arrived
    assert scalar(oracle, "SELECT acknowledged_at IS NULL FROM ingestion.runs") is True
    stack.clock.advance(timedelta(seconds=1801))  # the watchdog's deadline
    drain(stack, oracle, follow_retries=False)
    assert version_status(oracle, version_id) == "ready"  # re-published, applied, acknowledged
    assert scalar(oracle, "SELECT redeliveries FROM ingestion.runs") == 1
    assert scalar(oracle, "SELECT acknowledged_at IS NOT NULL FROM ingestion.runs") is True
    assert audit_actions(oracle).count("documents.version_outcome_recorded") == 1


def test_a_stalled_run_whose_job_vanished_is_dead_lettered_by_the_watchdog_not_replayed(
    database: Database, oracle: Engine
) -> None:
    s = Stack(database, None, [ScriptedStage("fake_one")])
    try:
        _, version_id = s.upload()
        s.worker.run_once()  # events queue: start-pipeline job -> enqueues the ingestion job

        # the ingestion job starts (creating the run + watchdog), the process dies, and the job
        # row ends up dead (e.g. a last-attempt lease expiry): no handler ever saw the end
        stage: ScriptedStage = s.stages[0]  # type: ignore[assignment]
        stage._steps.insert(0, Crash())  # noqa: SLF001
        with pytest.raises(Crash):
            s.worker.run_once()
        with oracle.begin() as conn:
            conn.execute(
                text("UPDATE platform.jobs SET status = 'dead' WHERE job_type = 'ingestion:run'")
            )
        assert scalar(oracle, "SELECT status FROM ingestion.runs") == "running"
        s.clock.advance(timedelta(seconds=1801))
        drain(s, oracle, follow_retries=False)
        assert scalar(oracle, "SELECT status FROM ingestion.runs") == "dead_lettered"
        assert scalar(oracle, "SELECT error_code FROM ingestion.runs") == "watchdog_stalled"
        assert version_status(oracle, version_id) == "pending"
        assert (
            scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE job_type = 'ingestion:run'")
            == 1
        )
    finally:
        s.close()


def test_a_crashed_workers_job_is_reclaimed_by_the_worker_loop_and_finishes(
    stack: Stack, oracle: Engine
) -> None:
    _, version_id = stack.upload()
    stack.worker.run_once()  # events queue -> start-pipeline job -> the ingestion job is queued
    (job,) = stack.platform.job_leases.claim(
        "ingestion", 1, "crashed-worker", timedelta(seconds=60)
    )
    stack.clock.advance(timedelta(seconds=120))  # its lease expires; nobody heartbeats
    drain(stack, oracle)  # the worker loop reclaims it (periodic reclaim) and runs it
    assert version_status(oracle, version_id) == "ready"
    # the crashed worker comes back to life: every one of its operations fails closed
    store = stack.platform.job_leases
    assert not store.complete(job.id, "crashed-worker", job.fencing_token)
    assert not store.fail(job.id, "crashed-worker", job.fencing_token, "x")
    assert (
        scalar(oracle, "SELECT status FROM platform.jobs WHERE job_type = 'ingestion:run'")
        == "succeeded"
    )


# ---- tenancy -----------------------------------------------------------------------------------


def test_two_tenants_flow_independently_and_never_see_each_others_runs(
    stack: Stack, oracle: Engine
) -> None:
    _, v1 = stack.upload(T1)
    admin = SystemContext(T2, "ingestion-e2e")
    other = stack.identity.register_principal.execute(
        admin, RegisterPrincipalCommand(PrincipalKind.USER, "Zed")
    ).principal_id
    view = stack.docs.upload_document.execute(
        TenantContext(T2, other, RequestId(UUID(int=10))), UploadDocumentCommand("Other", PDF, BODY)
    )
    drain(stack, oracle)
    assert version_status(oracle, v1) == "ready"
    assert version_status(oracle, view.version_id.value) == "ready"
    rows = fetch(oracle, "SELECT tenant_id, status FROM ingestion.runs")
    assert sorted((str(t), s) for t, s in rows) == sorted(
        [(str(T1.value), "completed"), (str(T2.value), "completed")]
    )
    mine = stack.ingestion.get_ingestion_metrics.execute(stack.ctx(T1))
    assert dict(mine.runs_by_status) == {"completed": 1}
    status = stack.ingestion.get_ingestion_status.execute(
        stack.ctx(T1), VersionQuery(VersionRef(view.version_id.value))
    )
    assert status is None  # tenant 1 cannot see tenant 2's run


def test_a_forged_job_with_another_tenants_version_changes_nothing_there(
    stack: Stack, oracle: Engine
) -> None:
    """A job row of tenant T2 that names tenant T1's version creates at most an inert T2 run:
    Documents verifies the version under RLS and finds none, so T1's version is untouched."""
    from lda.modules.ingestion.api import ingestion_job_spec
    from lda.platform.database.jobs.queue import PostgresJobQueue
    from lda.platform.database.transaction import TenantTransaction

    document_id, version_id = stack.upload(T1)
    spec = ingestion_job_spec(document_id, version_id)
    with TenantTransaction(stack.engine, T2) as tx:
        PostgresJobQueue(tx, stack.clock, stack.platform.ids).enqueue(
            dataclasses.replace(spec, unique_key="forged")
        )
        tx.commit()
    drain(stack, oracle)
    forged = fetch(
        oracle, "SELECT status FROM ingestion.runs WHERE tenant_id = :t", t=str(T2.value)
    )
    assert forged == []  # inert: its run lived in T2, Documents found no such version, and the
    # handler purged it again; nothing of T1 was touched
    assert version_status(oracle, version_id) == "ready"  # T1's version got ITS OWN run's outcome
    assert scalar(oracle, "SELECT count(*) FROM documents.versions") == 1
    assert (
        audit_actions(oracle).count("documents.version_outcome_recorded") == 1
    )  # T2's did nothing
