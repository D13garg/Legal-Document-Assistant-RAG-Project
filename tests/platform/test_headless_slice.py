"""ADR-045 headless vertical slice, with a SEPARATE worker process.

What is real here: the worker is its own OS process (the real `run_worker`), the object store is a
real S3 HTTP endpoint in a third process (moto's standalone server: an EMULATOR, not MinIO), the
database is embedded PostgreSQL reached as the least-privilege runtime role, and every use case is
the production one. What is NOT real: the embedder is the retrieval TEST DOUBLE (no development
embedder exists until the owner rules, ADR-045), the scanner never sees bytes, and the LLM is the
extractive development provider. This proves orchestration, authorization, tenant isolation and
citation/provenance mechanics, not retrieval quality or any production integration.
"""

from __future__ import annotations

import base64
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import boto3
import pytest
import test_shared_object_store as store_support
from sqlalchemy import create_engine, text

from lda.bootstrap.ai_gateway import DEVELOPMENT_MODEL
from lda.bootstrap.headless import (
    SliceError,
    SliceReport,
    require_shared_object_store,
    run_slice,
)
from lda.bootstrap.provisioning import RUNTIME_MIGRATIONS
from lda.modules.retrieval.testing import FakeEmbedder
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError, ExternalProviderError
from platform_support import Database, provisioned_database

endpoint = store_support.endpoint  # the real S3 server process fixture
ROOT = Path(__file__).resolve().parents[2]
KEY = base64.urlsafe_b64encode(b"headless-test-signing-key-32byte").decode().rstrip("=")
TENANT = UUID(int=0x4EAD1E55)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_slice", "lda_app_slice", list(RUNTIME_MIGRATIONS)) as db:
        yield db


def env(db: Database, url: str, **extra: str) -> dict[str, str]:
    return {
        "LDA_DATABASE__URL": db.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": store_support.BUCKET,
        "LDA_OBJECT_STORAGE__ENDPOINT_URL": url,
        "LDA_OBJECT_STORAGE__ACCESS_KEY_ID": store_support.ACCESS,
        "LDA_OBJECT_STORAGE__SECRET_ACCESS_KEY": store_support.SECRET,
        "LDA_JOBS__QUEUES": '{"documents": 1, "events": 1, "ingestion": 1}',
        "LDA_JOBS__POLL_INTERVAL_SECONDS": "0.1",
        "LDA_CHUNKING__MAX_CHARACTERS": "300",
        "LDA_CHUNKING__MIN_CHARACTERS": "20",
        "LDA_RETRIEVAL__CANDIDATE_POOL": "5",
        "LDA_RETRIEVAL__MAX_CANDIDATES": "50",
        "LDA_ACCESS_CONTROL__PROOF_KEY_ID": "primary",
        "LDA_ACCESS_CONTROL__PROOF_KEY": KEY,
        "LDA_GROUNDED_GENERATION__MODEL": DEVELOPMENT_MODEL,
        "LDA_DEVELOPMENT__ENVIRONMENT": "development",
        "LDA_DEV_OPERATOR__ENVIRONMENT": "development",
        "LDA_DEV_OPERATOR__TENANT_ID": str(TENANT),
        **extra,
    }


def start_worker(environment: dict[str, str]) -> subprocess.Popen[str]:
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


def stop_worker(process: subprocess.Popen[str]) -> tuple[int, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    out, err = process.communicate(timeout=60)
    return process.returncode, out + err


def scalar(db: Database, sql: str, **params: object) -> object:
    engine = create_engine(db.admin_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


# ---- the slice -------------------------------------------------------------------------------


def test_the_full_slice_runs_through_a_separate_worker_process_and_a_shared_s3_endpoint(
    database: Database, endpoint: str
) -> None:
    environment = env(database, endpoint)
    worker = start_worker(environment)
    try:
        report = run_slice(
            ConfigurationLoader(environment),
            FakeEmbedder(),
            timeout_seconds=90,
            poll_seconds=0.2,
        )
    finally:
        status, output = stop_worker(worker)
    assert status == 0, output  # SIGTERM -> clean stop of the real worker process
    assert isinstance(report, SliceReport)
    assert report.outcome == "answered" and report.citations >= 1
    assert report.record_id is not None and report.other_tenant_denied is True

    # evidence in persisted state (admin connection is the test oracle only)
    assert (
        scalar(
            database,
            "SELECT count(*) FROM citations.generation_records WHERE tenant_id = :t",
            t=TENANT,
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT count(*) FROM documents.versions WHERE document_id = :d AND status = 'ready'",
            d=report.document_id,
        )
        == 1
    )
    chunk_sets = scalar(
        database, "SELECT count(*) FROM chunking.chunk_sets WHERE tenant_id = :t", t=TENANT
    )
    vectors = scalar(
        database, "SELECT count(*) FROM retrieval.chunk_vectors WHERE tenant_id = :t", t=TENANT
    )
    assert isinstance(chunk_sets, int) and chunk_sets >= 1  # chunked by the worker
    assert isinstance(vectors, int) and vectors >= 1  # indexed by the worker
    assert scalar(database, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0
    # the stored original really is in the shared S3 endpoint, under the tenant prefix
    listing = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url=endpoint,
        aws_access_key_id=store_support.ACCESS,
        aws_secret_access_key=store_support.SECRET,
    ).list_objects_v2(Bucket=store_support.BUCKET, Prefix=f"tenant/{TENANT}/")
    assert listing.get("KeyCount", 0) >= 1
    # nothing sensitive in the worker's streams or in the report
    assert store_support.SECRET not in output and "postgresql" not in output
    assert "ninety days" not in repr(report)
    # tenant B's attempt left nothing behind: no record, message, grant or document
    for table in (
        "citations.generation_records",
        "conversations.messages",
        "access_control.grants",
        "documents.documents",
    ):
        other = scalar(database, f"SELECT count(*) FROM {table} WHERE tenant_id <> :t", t=TENANT)  # noqa: S608
        assert other == 0, table
    # the first tenant's grant is exactly one read of the one document
    rows = scalar(
        database,
        "SELECT count(*) FROM access_control.grants WHERE tenant_id = :t AND resource_id = :d "
        "AND action = 'documents.read_document'",
        t=TENANT,
        d=str(report.document_id),
    )
    assert rows == 1
    assert (
        scalar(
            database, "SELECT count(*) FROM access_control.grants WHERE tenant_id <> :t", t=TENANT
        )
        == 0
    )


def test_without_a_worker_the_slice_times_out_with_a_stage_code_and_no_content(
    database: Database, endpoint: str
) -> None:
    with pytest.raises(SliceError) as caught:
        run_slice(
            ConfigurationLoader(env(database, endpoint)),
            FakeEmbedder(),
            timeout_seconds=2.0,
            poll_seconds=0.2,
        )
    assert caught.value.code == "slice_timeout"
    assert "ninety" not in repr(caught.value) + caught.value.detail


# ---- fail-closed start-up --------------------------------------------------------------------


def test_the_slice_refuses_without_the_development_guard_before_any_pool_exists(
    database: Database, endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a pool was built")

    monkeypatch.setattr("lda.bootstrap.headless.build_platform", boom)
    for override in (
        {"LDA_DEVELOPMENT__ENVIRONMENT": "production"},
        {"LDA_DEV_OPERATOR__ENVIRONMENT": "test"},
    ):
        with pytest.raises(ConfigurationError):
            run_slice(ConfigurationLoader(env(database, endpoint, **override)), FakeEmbedder())


def test_an_in_memory_style_store_configuration_is_refused(
    database: Database, endpoint: str
) -> None:
    for missing in ("LDA_OBJECT_STORAGE__ENDPOINT_URL", "LDA_OBJECT_STORAGE__ACCESS_KEY_ID"):
        environment = env(database, endpoint)
        del environment[missing]
        with pytest.raises(ConfigurationError) as caught:
            require_shared_object_store(ConfigurationLoader(environment))
        assert caught.value.code == "shared_object_store_required"


def test_an_unavailable_store_fails_clearly_before_any_work(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("a pool was built")

    monkeypatch.setattr("lda.bootstrap.headless.build_platform", boom)
    dead = f"http://127.0.0.1:{store_support.free_port()}"
    with pytest.raises(ExternalProviderError) as caught:
        run_slice(ConfigurationLoader(env(database, dead)), FakeEmbedder())
    assert caught.value.code == "object_store_unavailable"
    assert store_support.SECRET not in repr(caught.value) + caught.value.detail
