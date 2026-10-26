"""ADR-044 worker: registration, queue validation, shutdown, disposal, and a real process.

Real PostgreSQL (embedded, trust auth) and the real builders. The scanner and embedder are test
doubles; no object storage is contacted. This does NOT verify Compose, MinIO or an end-to-end flow.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.provisioning import RUNTIME_MIGRATIONS
from lda.bootstrap.worker import (
    WorkerAdapters,
    build_worker_runtime,
    merge_handlers,
    run_worker,
    select_adapters,
)
from lda.modules.ingestion.testing import ScriptedScanner
from lda.modules.retrieval.testing import FakeEmbedder
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.database.jobs.worker import JobWorker
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.errors import ConfigurationError
from lda.shared.ports.jobs import JobSpec
from platform_support import Database, enqueue_job, provisioned_database

ROOT = Path(__file__).resolve().parents[2]
KEY = base64.urlsafe_b64encode(b"worker-test-signing-key-32-bytes!").decode().rstrip("=")
ALL_QUEUES = '{"documents": 1, "events": 1, "ingestion": 1}'
EXPECTED_HANDLERS = {
    "documents:purge",
    "documents:upload_intent",
    "event:chunking.purge_chunks",
    "event:document_processing.purge_extractions",
    "event:documents.cancel_pipeline",
    "event:documents.clear_pipeline_records",
    "event:documents.record_failed",
    "event:documents.record_quarantined",
    "event:documents.record_ready",
    "event:documents.start_pipeline",
    "event:retrieval.purge_index",
    "ingestion:run",
    "ingestion:watchdog",
}


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_worker", "lda_app_worker", list(RUNTIME_MIGRATIONS)) as db:
        yield db


def env(db: Database, queues: str = ALL_QUEUES, **extra: str) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": queues,
        "LDA_JOBS__POLL_INTERVAL_SECONDS": "0.1",
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        **extra,
    }


def adapters() -> WorkerAdapters:
    return WorkerAdapters(ScriptedScanner(), FakeEmbedder())


class Disposals:
    """Counts pool disposals by wrapping `build_platform` (the real platform is still built)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        self.built = 0
        real = build_platform

        def wrapped(config: ConfigurationLoader, **kwargs: object) -> PlatformContainer:
            platform = real(config, **kwargs)  # type: ignore[arg-type]
            self.built += 1
            original = platform.dispose

            def counting() -> None:
                self.count += 1
                original()

            return dataclasses.replace(platform, dispose=counting)

        monkeypatch.setattr("lda.bootstrap.worker.build_platform", wrapped)


def due_job(job_type: str = "no_such_job") -> JobSpec:
    return JobSpec(
        queue="events",
        job_type=job_type,
        max_attempts=1,
        run_at=datetime(2020, 1, 1, tzinfo=UTC),
    )


def job_status(db: Database) -> list[tuple[str, int]]:
    engine = create_engine(db.admin_url)
    try:
        with engine.connect() as conn:
            return [
                tuple(r) for r in conn.execute(text("SELECT status, attempts FROM platform.jobs"))
            ]
    finally:
        engine.dispose()


def clear_jobs(db: Database) -> None:
    engine = create_engine(db.admin_url)
    try:
        with engine.begin() as conn:  # test-owned throwaway database only
            conn.execute(text("DELETE FROM platform.jobs"))
    finally:
        engine.dispose()


def enqueue(db: Database, spec: JobSpec) -> None:
    engine = create_engine(db.app_url)
    try:
        enqueue_job(engine, FakeClock(), SequentialIdGenerator(), 1, spec)
    finally:
        engine.dispose()


def wait_for(predicate: object, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.05)
    return False


# ---- registration and queue validation -------------------------------------------------------


def test_the_worker_registers_exactly_the_expected_handlers_and_queues(database: Database) -> None:
    runtime = build_worker_runtime(ConfigurationLoader(env(database)), adapters())
    try:
        assert set(runtime.handler_types) == EXPECTED_HANDLERS
        assert runtime.queues == ("documents", "events", "ingestion")
    finally:
        runtime.dispose()


@pytest.mark.parametrize("missing", ["documents", "events", "ingestion"])
def test_a_missing_required_queue_refuses_start_up_and_releases_the_pool(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    queues = json.dumps({q: 1 for q in ("documents", "events", "ingestion") if q != missing})
    disposals = Disposals(monkeypatch)
    status = run_worker(
        ConfigurationLoader(env(database, queues)), adapters(), install_signals=False
    )
    assert status == 2
    assert f"{missing}_queue_not_configured" in capsys.readouterr().err
    assert disposals.built == 1 and disposals.count == 1


def test_duplicate_handler_keys_are_refused_not_overwritten() -> None:
    class H:
        def handle(self, *_: object) -> object:  # pragma: no cover - never called
            raise AssertionError

    with pytest.raises(ConfigurationError) as caught:
        merge_handlers({"a": H(), "b": H()}, {"b": H()})  # type: ignore[dict-item]
    assert caught.value.code == "duplicate_job_handler" and caught.value.detail == "b"


# ---- start-up refusals -----------------------------------------------------------------------


def test_without_a_usable_adapter_set_the_worker_refuses_before_building_anything(
    database: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    disposals = Disposals(monkeypatch)
    # no set named, an unknown name, and the development set WITHOUT the development guard
    cases: tuple[tuple[dict[str, str], str], ...] = (
        ({}, "configuration_invalid"),
        ({"LDA_WORKER__ADAPTER_SET": "unknown"}, "worker_adapter_set_unavailable"),
        ({"LDA_WORKER__ADAPTER_SET": "development"}, "configuration_invalid"),
    )
    for extra, code in cases:
        config = ConfigurationLoader(env(database, **extra))
        assert run_worker(config, None, install_signals=False) == 2
        assert code in capsys.readouterr().err
        assert disposals.built == 0  # no pool was ever created
    with pytest.raises(ConfigurationError):
        select_adapters(ConfigurationLoader(env(database, LDA_WORKER__ADAPTER_SET="unknown")))


def test_missing_configuration_refuses_with_a_code_and_no_secret(
    database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = env(database)
    del bad["LDA_ACCESS_CONTROL__PROOF_KEY"]
    assert run_worker(ConfigurationLoader(bad), adapters(), install_signals=False) == 2
    err = capsys.readouterr().err
    assert "refused" in err and KEY not in err and "postgresql" not in err


# ---- shutdown and disposal -------------------------------------------------------------------


def test_a_preset_stop_returns_zero_and_disposes_the_pool(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposals = Disposals(monkeypatch)
    stop = threading.Event()
    stop.set()
    assert (
        run_worker(ConfigurationLoader(env(database)), adapters(), stop=stop, install_signals=False)
        == 0
    )
    assert disposals.built == 1 and disposals.count == 1


def test_sigterm_and_sigint_stop_the_loop_and_restore_previous_handlers(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposals = Disposals(monkeypatch)
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    for sig in (signal.SIGTERM, signal.SIGINT):
        timer = threading.Timer(1.0, lambda s=sig: os.kill(os.getpid(), s))
        timer.start()
        status = run_worker(ConfigurationLoader(env(database)), adapters())
        timer.join()
        assert status == 0
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    assert disposals.built == 2 and disposals.count == 2


def test_a_runtime_failure_exits_one_disposes_and_hides_the_message(
    database: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    disposals = Disposals(monkeypatch)

    def boom(self: JobWorker) -> int:
        raise RuntimeError("connection lost password=topsecret")

    monkeypatch.setattr(JobWorker, "run_once", boom)
    assert run_worker(ConfigurationLoader(env(database)), adapters(), install_signals=False) == 1
    err = capsys.readouterr().err
    assert "unhandled_exception" in err and "topsecret" not in err
    assert disposals.count == 1


def test_the_worker_claims_a_due_job_through_the_real_queue_and_stops_claiming_on_stop(
    database: Database,
) -> None:
    clear_jobs(database)
    enqueue(database, due_job())
    stop = threading.Event()
    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(
            run_worker(
                ConfigurationLoader(env(database)), adapters(), stop=stop, install_signals=False
            )
        )
    )
    thread.start()
    try:
        assert wait_for(lambda: job_status(database) and job_status(database)[0][0] != "queued")
    finally:
        stop.set()
        thread.join(timeout=30)
    assert results == [0]
    status, attempts = job_status(database)[0]
    assert attempts >= 1 and status != "queued"  # no handler exists -> failed, per JobWorker

    clear_jobs(database)
    enqueue(database, due_job("no_such_job_after_stop"))  # the worker has stopped
    time.sleep(0.5)
    assert job_status(database)[0][0] == "queued"  # nothing claims work after shutdown


# ---- a real, separate process ----------------------------------------------------------------


def _spawn(environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, str(ROOT / "tests/platform/worker_process_harness.py")],
        cwd=ROOT,
        env={
            **os.environ,
            **environment,
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_a_separate_worker_process_claims_work_and_exits_zero_on_sigterm(
    database: Database,
) -> None:
    clear_jobs(database)
    enqueue(database, due_job())
    process = _spawn(env(database))
    try:
        claimed = wait_for(
            lambda: job_status(database) and job_status(database)[0][0] != "queued", timeout=60
        )
        assert claimed, "the separate process never claimed the job"
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, err
    assert "postgresql" not in out + err and KEY not in out + err
    engine = create_engine(database.admin_url)
    try:
        with engine.connect() as conn:
            left = conn.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE usename = 'lda_app_worker'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert left == 0  # the process released every pooled connection


def test_the_real_entry_point_refuses_without_an_adapter_set(database: Database) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "apps.worker"],
        cwd=ROOT,
        env={
            **os.environ,
            **env(database),
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 2
    assert "worker_adapter_set_unavailable" in done.stderr or "configuration_invalid" in done.stderr
