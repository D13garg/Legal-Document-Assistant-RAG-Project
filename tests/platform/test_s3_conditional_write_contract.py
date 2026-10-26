"""The create-only `put` relies on S3 conditional writes (`IfNoneMatch="*"`), which botocore only
models from 1.35.2. A floor below that makes every create-only write fail parameter validation,
which the adapter would then misreport as `object_store_unavailable`.

Scope: parameter validation against the real botocore service model (no network, no emulator) and
the declared dependency floor. It does NOT prove that any particular server honours the condition.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import boto3
import pytest
from botocore.stub import Stubber

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.s3 import S3ObjectStore
from lda.shared.ports.object_store import TenantScopedKey
from platform_support import tenant

MIN_BOTO3 = (1, 35, 2)
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _store(monkeypatch: pytest.MonkeyPatch) -> tuple[S3ObjectStore, Stubber, TenantScopedKey]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    client = boto3.client("s3", region_name="us-east-1")
    store = S3ObjectStore(client, ObjectStorageSettings(bucket="b"))
    return store, Stubber(client), TenantScopedKey(tenant(1), "documents/d1/original")


def test_create_only_put_sends_if_none_match(monkeypatch: pytest.MonkeyPatch) -> None:
    store, stub, key = _store(monkeypatch)
    stub.add_response(
        "put_object",
        {},
        {
            "Bucket": "b",
            "Key": key.render(),
            "Body": b"x",
            "ContentType": "text/plain",
            "IfNoneMatch": "*",
        },
    )
    with stub:
        store.put(key, b"x", content_type="text/plain")
        stub.assert_no_pending_responses()


def test_overwrite_put_sends_no_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    store, stub, key = _store(monkeypatch)
    stub.add_response(
        "put_object",
        {},
        {"Bucket": "b", "Key": key.render(), "Body": b"x", "ContentType": "text/plain"},
    )
    with stub:
        store.put(key, b"x", content_type="text/plain", overwrite=True)
        stub.assert_no_pending_responses()


def test_declared_boto3_floor_supports_conditional_put() -> None:
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["dependencies"]
    specs = [d.replace(" ", "") for d in declared if d.replace(" ", "").startswith("boto3")]
    assert len(specs) == 1
    assert specs[0].startswith("boto3>=")
    floor = tuple(int(p) for p in specs[0].removeprefix("boto3>=").split(".")[:3])
    assert floor >= MIN_BOTO3, f"boto3 floor {floor} predates PutObject IfNoneMatch {MIN_BOTO3}"
