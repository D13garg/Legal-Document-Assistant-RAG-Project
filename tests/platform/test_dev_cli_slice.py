"""ADR-045 D3 resolved: the REAL command-line entry points run the whole development slice.

`python -m apps.worker` and `python -m apps.dev_slice` run as separate OS processes against
embedded PostgreSQL (least-privilege runtime role), a real S3 HTTP endpoint in a third process
(moto's standalone server: an EMULATOR, not MinIO, not Docker Compose), with the approved offline
`DevelopmentEmbedder`, the always-CLEAN development scanner and the extractive development LLM.

This proves orchestration, authorization, tenant isolation, indexing and citation mechanics. It does
not prove retrieval quality, malware scanning, a real language model, MinIO or Compose behavior.
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

import pytest
import test_shared_object_store as store_support
from sqlalchemy import create_engine, text

from lda.bootstrap.ai_gateway import DEVELOPMENT_MODEL
from lda.bootstrap.provisioning import RUNTIME_MIGRATIONS
from lda.modules.retrieval.api import DevelopmentEmbedder
from lda.modules.retrieval.testing.doubles import FAKE_IDENTITY
from platform_support import Database, provisioned_database

endpoint = store_support.endpoint  # the real S3 server process fixture
ROOT = Path(__file__).resolve().parents[2]
KEY = base64.urlsafe_b64encode(b"cli-slice-test-signing-key-32byt").decode().rstrip("=")
TENANT = UUID(int=0xC11511CE)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_cli", "lda_app_cli", list(RUNTIME_MIGRATIONS)) as db:
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
        "LDA_WORKER__ADAPTER_SET": "development",
        **extra,
    }


def child_env(environment: dict[str, str]) -> dict[str, str]:
    return {
        **os.environ,
        **environment,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
    }


def start(module: str, environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=ROOT,
        env=child_env(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish(process: subprocess.Popen[str], timeout: float = 120) -> tuple[int, str, str]:
    out, err = process.communicate(timeout=timeout)
    return process.returncode, out, err


def stop(process: subprocess.Popen[str]) -> tuple[int, str, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    return finish(process, 60)


def scalar(db: Database, sql: str, **params: object) -> object:
    engine = create_engine(db.admin_url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


# ---- the happy path, entirely through the real commands --------------------------------------


def test_the_real_worker_and_slice_commands_run_the_whole_slice(
    database: Database, endpoint: str
) -> None:
    environment = env(database, endpoint)
    worker = start("apps.worker", environment)
    try:
        slice_status, slice_out, slice_err = finish(start("apps.dev_slice", environment))
    finally:
        worker_status, worker_out, worker_err = stop(worker)
    assert slice_status == 0, slice_err
    assert worker_status == 0, worker_err  # SIGTERM -> clean stop of the real worker command
    assert "outcome answered" in slice_out and "other tenant denied: True" in slice_out

    dev_index = DevelopmentEmbedder().identity.index_id
    # chunked and indexed by the worker with the development identity, and ONLY that identity
    vectors = scalar(
        database, "SELECT count(*) FROM retrieval.chunk_vectors WHERE tenant_id = :t", t=TENANT
    )
    assert isinstance(vectors, int) and vectors >= 1
    assert (
        scalar(
            database,
            "SELECT count(*) FROM retrieval.chunk_vectors WHERE tenant_id = :t AND index_id <> :i",
            t=TENANT,
            i=dev_index,
        )
        == 0
    )
    assert (
        scalar(
            database,
            "SELECT count(*) FROM retrieval.chunk_vectors WHERE index_id = :i",
            i=FAKE_IDENTITY.index_id,
        )
        == 0
    )
    # the driver queried with the same identity: an answer exists, so the index was found
    assert (
        scalar(
            database,
            "SELECT count(*) FROM citations.generation_records WHERE tenant_id = :t",
            t=TENANT,
        )
        == 1
    )
    assert scalar(database, "SELECT count(*) FROM documents.versions WHERE status = 'ready'") == 1
    assert scalar(database, "SELECT count(*) FROM platform.jobs WHERE status = 'dead'") == 0
    # exactly one read grant, in the first tenant only
    assert (
        scalar(
            database,
            "SELECT count(*) FROM access_control.grants WHERE tenant_id = :t AND action = 'documents.read_document'",
            t=TENANT,
        )
        == 1
    )
    assert (
        scalar(
            database, "SELECT count(*) FROM access_control.grants WHERE tenant_id <> :t", t=TENANT
        )
        == 0
    )
    # the second tenant left no document, message or generation record
    for table in ("documents.documents", "conversations.messages", "citations.generation_records"):
        sql = f"SELECT count(*) FROM {table} WHERE tenant_id <> :t"  # noqa: S608 - fixed names
        assert scalar(database, sql, t=TENANT) == 0, table
    # nothing sensitive printed by either process
    everything = slice_out + slice_err + worker_out + worker_err
    for secret in (store_support.SECRET, KEY, "ninety days", "postgresql"):
        assert secret not in everything


# ---- fail closed, through the real commands --------------------------------------------------


def test_the_slice_times_out_when_no_worker_runs(database: Database, endpoint: str) -> None:
    status, out, err = finish(
        start(
            "apps.dev_slice",
            env(
                database,
                endpoint,
                LDA_DEV_SLICE__TIMEOUT_SECONDS="2",
                LDA_DEV_SLICE__POLL_SECONDS="0.2",
            ),
        )
    )
    assert status == 1 and "slice_timeout" in err
    assert "ninety" not in out + err


def test_a_missing_object_store_service_fails_before_any_work(database: Database) -> None:
    dead = f"http://127.0.0.1:{store_support.free_port()}"
    status, out, err = finish(start("apps.dev_slice", env(database, dead)))
    assert status == 2 and "object_store_unavailable" in err
    assert store_support.SECRET not in out + err and dead not in out + err


def test_a_missing_bucket_fails_with_its_own_code(database: Database, endpoint: str) -> None:
    status, _out, err = finish(
        start(
            "apps.dev_slice", env(database, endpoint, LDA_OBJECT_STORAGE__BUCKET="no-such-bucket")
        )
    )
    assert status == 2 and "object_store_bucket_missing" in err


@pytest.mark.parametrize("module", ["apps.worker", "apps.dev_slice"])
@pytest.mark.parametrize(
    "override",
    [
        {"LDA_DEVELOPMENT__ENVIRONMENT": "production"},
        {"LDA_WORKER__ADAPTER_SET": "production"},
        {"LDA_WORKER__ADAPTER_SET": "fake"},
    ],
)
def test_unsafe_environment_or_unknown_adapter_set_is_refused_with_exit_2(
    database: Database, endpoint: str, module: str, override: dict[str, str]
) -> None:
    status, out, err = finish(start(module, env(database, endpoint, **override)))
    assert status == 2 and "refused" in err
    assert store_support.SECRET not in out + err and KEY not in out + err


def test_a_non_local_database_is_refused_by_the_development_guard(endpoint: str) -> None:
    environment = env(
        Database("unused", "postgresql+psycopg://app:hunter2@db.prod.example.com/lda"),
        endpoint,
    )
    status, out, err = finish(start("apps.worker", environment))
    assert status == 2 and "development_requires_local_database" in err
    assert "hunter2" not in out + err


def test_the_real_worker_refuses_to_start_without_a_required_queue(
    database: Database, endpoint: str
) -> None:
    queues = '{"documents": 1, "events": 1}'  # no ingestion queue
    status, _out, err = finish(
        start("apps.worker", env(database, endpoint, LDA_JOBS__QUEUES=queues))
    )
    assert status == 2 and "ingestion" in err and "queue" in err
