"""ADR-045: a document stored by one OS process is readable by another through a real S3 HTTP API.

The server is moto's standalone S3 server in its own process: a real S3-compatible HTTP endpoint,
but an EMULATOR, not MinIO, and not Docker Compose. It proves the unchanged production adapter
(`create_object_store`) works across processes against an `endpoint_url`; it does not prove MinIO
behavior, MinIO's conditional-write support, or the Compose service definition.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import boto3
import pytest

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.factory import create_object_store
from lda.platform.storage.readiness import check_bucket_ready
from lda.shared.kernel.errors import ConflictError, ExternalProviderError, NotFoundError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.object_store import TenantScopedKey

ROOT = Path(__file__).resolve().parents[2]
ACCESS, SECRET = "devaccesskey", "devsecretkey-do-not-print"
BUCKET = "lda-dev-documents"
TENANT = TenantId(UUID(int=0x5301))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def settings(endpoint: str, bucket: str = BUCKET) -> ObjectStorageSettings:
    return ObjectStorageSettings(
        bucket=bucket,
        endpoint_url=endpoint,
        access_key_id=ACCESS,  # type: ignore[arg-type]
        secret_access_key=SECRET,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    port = free_port()
    server = subprocess.Popen(
        [str(Path(sys.executable).parent / "moto_server"), "-H", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("the S3 server process did not start")
        boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=url,
            aws_access_key_id=ACCESS,
            aws_secret_access_key=SECRET,
        ).create_bucket(Bucket=BUCKET)
        yield url
    finally:
        server.send_signal(signal.SIGTERM)
        server.wait(timeout=20)


def run_child(code: str, url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "src")]),
            "ENDPOINT": url,
            "BUCKET": BUCKET,
            "ACCESS": ACCESS,
            "SECRET": SECRET,
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


CHILD_PRELUDE = """
import os
from uuid import UUID
from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.factory import create_object_store
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.object_store import TenantScopedKey
store = create_object_store(ObjectStorageSettings(bucket=os.environ["BUCKET"],
    endpoint_url=os.environ["ENDPOINT"], access_key_id=os.environ["ACCESS"],
    secret_access_key=os.environ["SECRET"]))
key = TenantScopedKey(TenantId(UUID(int=0x5301)), "documents/agreement.txt")
"""


def test_a_document_written_by_one_process_is_read_by_another(endpoint: str) -> None:
    writer = run_child(
        CHILD_PRELUDE + 'store.put(key, b"shared across processes", content_type="text/plain")\n',
        endpoint,
    )
    assert writer.returncode == 0, writer.stderr
    reader = run_child(CHILD_PRELUDE + "print(store.get(key).decode())\n", endpoint)
    assert reader.returncode == 0, reader.stderr
    assert reader.stdout.strip() == "shared across processes"
    assert SECRET not in writer.stdout + writer.stderr + reader.stdout + reader.stderr


def test_the_unchanged_adapter_keeps_its_conditional_write_semantics(endpoint: str) -> None:
    store = create_object_store(settings(endpoint))
    key = TenantScopedKey(TENANT, "documents/once.txt")
    store.put(key, b"first", content_type="text/plain")
    assert store.exists(key) and store.get(key) == b"first"
    with pytest.raises(ConflictError):
        store.put(key, b"second", content_type="text/plain")  # create-only, as in production
    assert store.get(key) == b"first"
    store.put(key, b"second", content_type="text/plain", overwrite=True)
    assert store.get(key) == b"second"


def test_another_tenant_key_is_a_different_object(endpoint: str) -> None:
    store = create_object_store(settings(endpoint))
    mine = TenantScopedKey(TENANT, "documents/private.txt")
    store.put(mine, b"tenant one only", content_type="text/plain")
    other = TenantScopedKey(TenantId(UUID(int=0x5302)), "documents/private.txt")
    assert not store.exists(other)
    with pytest.raises(NotFoundError):
        store.get(other)


def test_readiness_accepts_an_existing_bucket(endpoint: str) -> None:
    check_bucket_ready(settings(endpoint))


def test_readiness_names_a_missing_bucket(endpoint: str) -> None:
    with pytest.raises(NotFoundError) as caught:
        check_bucket_ready(settings(endpoint, bucket="no-such-bucket-here"))
    assert caught.value.code == "object_store_bucket_missing"


def test_an_unreachable_store_fails_clearly_without_leaking_anything() -> None:
    dead = f"http://127.0.0.1:{free_port()}"  # nothing listens here
    with pytest.raises(ExternalProviderError) as caught:
        check_bucket_ready(settings(dead), timeout_seconds=1.0)
    assert caught.value.code == "object_store_unavailable"
    text = repr(caught.value) + caught.value.detail + str(caught.value)
    assert SECRET not in text and dead not in text and ACCESS not in text
    with pytest.raises(ExternalProviderError) as stored:
        create_object_store(settings(dead)).exists(TenantScopedKey(TENANT, "documents/x.txt"))
    assert stored.value.code == "object_store_unavailable"
