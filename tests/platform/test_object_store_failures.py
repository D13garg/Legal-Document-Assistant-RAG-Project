"""Object-store immutability, presign bounds and provider-failure mapping (no secrets in errors)."""

from __future__ import annotations

import typing
from collections.abc import Iterator

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.s3 import S3ObjectStore
from lda.platform.testing.doubles import InMemoryObjectStore
from lda.shared.kernel.errors import (
    ConflictError,
    ExternalProviderError,
    LdaError,
    NotFoundError,
)
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey
from platform_support import tenant

MAX = 900
BODY = b"PRIVILEGED-DOCUMENT-BODY"


def key(n: int = 1, path: str = "documents/d1/original") -> TenantScopedKey:
    return TenantScopedKey(tenant(n), path)


@pytest.fixture(params=["memory", "s3"])
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStore]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    if request.param == "memory":
        yield InMemoryObjectStore(presign_max_seconds=MAX)
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield S3ObjectStore(
            client, ObjectStorageSettings(bucket="test-bucket", presign_max_seconds=MAX)
        )


# ---- immutability ------------------------------------------------------------------------------


def test_a_refused_overwrite_leaves_the_original_bytes_untouched(store: ObjectStore) -> None:
    store.put(key(), b"original", content_type="text/plain")
    with pytest.raises(ConflictError) as exc:
        store.put(key(), b"tampered", content_type="text/plain")
    assert store.get(key()) == b"original"
    assert exc.value.detail == "" and "tenant/" not in str(exc.value)  # no key in the error


def test_explicit_overwrite_replaces_and_reports_the_new_size(store: ObjectStore) -> None:
    store.put(key(), b"v1", content_type="text/plain")
    assert store.put(key(), b"version-two", content_type="text/plain", overwrite=True).size == 11
    assert store.get(key()) == b"version-two"


def test_overwrite_flag_is_per_call_not_sticky(store: ObjectStore) -> None:
    store.put(key(), b"v1", content_type="text/plain")
    store.put(key(), b"v2", content_type="text/plain", overwrite=True)
    with pytest.raises(ConflictError):
        store.put(key(), b"v3", content_type="text/plain")
    assert store.get(key()) == b"v2"


def test_same_path_in_another_tenant_is_independent_not_a_conflict(store: ObjectStore) -> None:
    store.put(key(1), b"one", content_type="text/plain")
    store.put(key(2), b"two", content_type="text/plain")  # no ConflictError
    assert (store.get(key(1)), store.get(key(2))) == (b"one", b"two")
    store.delete(key(1))
    assert store.get(key(2)) == b"two"


def test_an_object_can_be_recreated_after_deletion(store: ObjectStore) -> None:
    store.put(key(), b"v1", content_type="text/plain")
    store.delete(key())
    store.put(key(), b"v2", content_type="text/plain")
    assert store.get(key()) == b"v2"


def test_empty_bodies_are_stored(store: ObjectStore) -> None:
    assert store.put(key(), b"", content_type="application/octet-stream").size == 0
    assert store.get(key()) == b"" and store.exists(key())


# ---- presigned URL expiry bounds ---------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0, -1, -3600, MAX + 1, 10**9])
def test_presign_rejects_out_of_range_expiry(store: ObjectStore, seconds: int) -> None:
    store.put(key(), b"x", content_type="text/plain")
    with pytest.raises(ValueError, match="out of allowed range"):
        store.presign_get(key(), expires_seconds=seconds)


@pytest.mark.parametrize("seconds", [1, 60, MAX])
def test_presign_accepts_the_inclusive_bounds(store: ObjectStore, seconds: int) -> None:
    store.put(key(), b"x", content_type="text/plain")
    url = store.presign_get(key(), expires_seconds=seconds)
    assert f"tenant/{tenant(1)}/" in url.replace("%2F", "/")


def test_presign_cap_comes_from_configuration() -> None:
    calls: list[object] = []

    class Client:
        def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
            calls.append(kwargs)
            return "https://example.invalid/signed"

    store = S3ObjectStore(Client(), ObjectStorageSettings(bucket="b", presign_max_seconds=60))
    assert store.presign_get(key(), expires_seconds=60)
    with pytest.raises(ValueError):
        store.presign_get(key(), expires_seconds=61)
    assert len(calls) == 1  # the rejected request never reached the provider SDK


@pytest.mark.parametrize("cap", [0, -1, 3601])
def test_configured_presign_cap_is_itself_bounded(cap: int) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ObjectStorageSettings(bucket="b", presign_max_seconds=cap)


# ---- provider failure mapping ------------------------------------------------------------------


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"msg {BODY!r}"}}, "Op")


class Scripted:
    """Stand-in for the boto3 client: every call raises the configured error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.put_args: dict[str, typing.Any] = {}

    def put_object(self, **kwargs: typing.Any) -> None:
        self.put_args = kwargs
        raise self.error

    def get_object(self, **kwargs: typing.Any) -> typing.Any:
        raise self.error

    def head_object(self, **kwargs: typing.Any) -> typing.Any:
        raise self.error

    def delete_object(self, **kwargs: typing.Any) -> typing.Any:
        raise self.error


def adapter(error: Exception) -> tuple[S3ObjectStore, Scripted]:
    client = Scripted(error)
    return S3ObjectStore(client, ObjectStorageSettings(bucket="b")), client


def assert_clean(exc: LdaError) -> None:
    rendered = f"{exc!s} {exc!r} {exc.detail}"
    assert (
        BODY.decode() not in rendered
        and "tenant/" not in rendered
        and "secret-host" not in rendered
    )


@pytest.mark.parametrize("code", ["PreconditionFailed", "412", "ConditionalRequestConflict"])
def test_put_precondition_failures_mean_the_object_exists(code: str) -> None:
    store, _ = adapter(client_error(code))
    with pytest.raises(ConflictError, match="object_exists") as exc:
        store.put(key(), BODY, content_type="text/plain")
    assert_clean(exc.value)


def test_put_provider_errors_become_external_provider_errors_with_only_the_code() -> None:
    store, _ = adapter(client_error("AccessDenied"))
    with pytest.raises(ExternalProviderError, match="object_store_unavailable") as exc:
        store.put(key(), BODY, content_type="text/plain")
    assert exc.value.detail == "AccessDenied"
    assert_clean(exc.value)


def test_transport_failures_become_external_provider_errors_without_the_endpoint() -> None:
    store, _ = adapter(EndpointConnectionError(endpoint_url="https://secret-host.internal"))
    operations: list[typing.Callable[[], object]] = [
        lambda: store.put(key(), BODY, content_type="text/plain"),
        lambda: store.get(key()),
        lambda: store.exists(key()),
        lambda: store.delete(key()),
    ]
    for operation in operations:
        with pytest.raises(ExternalProviderError, match="object_store_unavailable") as exc:
            operation()
        assert_clean(exc.value)


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound"])
def test_missing_objects_map_to_not_found_or_false(code: str) -> None:
    store, _ = adapter(client_error(code))
    with pytest.raises(NotFoundError, match="object_not_found"):
        store.get(key())
    assert store.exists(key()) is False


@pytest.mark.parametrize("code", ["AccessDenied", "403", "InternalError", "SlowDown"])
def test_other_provider_errors_are_never_mistaken_for_absence(code: str) -> None:
    store, _ = adapter(client_error(code))
    with pytest.raises(ExternalProviderError):
        store.get(key())
    with pytest.raises(ExternalProviderError):
        store.exists(key())  # a 403 must not read as "does not exist"
    with pytest.raises(ExternalProviderError):
        store.delete(key())


def test_immutability_precondition_is_sent_unless_overwrite_is_requested() -> None:
    store, client = adapter(client_error("PreconditionFailed"))
    with pytest.raises(ConflictError):
        store.put(key(), b"x", content_type="text/plain")
    assert client.put_args["IfNoneMatch"] == "*"
    assert client.put_args["Key"] == f"tenant/{tenant(1)}/documents/d1/original"
    store2, client2 = adapter(client_error("AccessDenied"))
    with pytest.raises(ExternalProviderError):
        store2.put(key(), b"x", content_type="text/plain", overwrite=True)
    assert "IfNoneMatch" not in client2.put_args


def test_kms_encryption_is_requested_when_configured() -> None:
    client = Scripted(client_error("AccessDenied"))
    store = S3ObjectStore(client, ObjectStorageSettings(bucket="b", kms_key_id="alias/docs"))
    with pytest.raises(ExternalProviderError):
        store.put(key(), b"x", content_type="text/plain")
    assert client.put_args["ServerSideEncryption"] == "aws:kms"
    assert client.put_args["SSEKMSKeyId"] == "alias/docs"


@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
@pytest.mark.parametrize("kms_key_id", [None, "alias/docs"])
def test_production_factory_presigns_with_sigv4_and_the_requested_expiry(
    monkeypatch: pytest.MonkeyPatch, region: str, kms_key_id: str | None
) -> None:
    from lda.platform.storage.factory import create_object_store

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    settings = ObjectStorageSettings(bucket="my-bucket", region=region, kms_key_id=kms_key_id)
    url = create_object_store(settings).presign_get(key(), expires_seconds=120)
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url  # legacy SigV2 cannot read SSE-KMS objects
    assert "X-Amz-Expires=120" in url
