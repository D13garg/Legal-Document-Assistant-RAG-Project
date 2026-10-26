"""Contract suites for ObjectStore and RateLimiter, run against every adapter (fake + real)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import timedelta

import boto3
import fakeredis
import pytest
from moto import mock_aws

from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.ratelimit.fail_open import FailOpenRateLimiter
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.ratelimit.redis_bucket import RedisTokenBucket
from lda.platform.storage.s3 import S3ObjectStore
from lda.platform.testing.doubles import FakeClock, InMemoryObjectStore
from lda.shared.kernel.errors import ConflictError, NotFoundError
from lda.shared.ports.object_store import ObjectStore, TenantScopedKey
from lda.shared.ports.rate_limiter import RateLimiter, RateLimitKey, RateLimitPolicy
from platform_support import tenant


@pytest.fixture(params=["memory", "s3"])
def store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[ObjectStore]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    if request.param == "memory":
        yield InMemoryObjectStore()
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="test-bucket")
        yield S3ObjectStore(client, ObjectStorageSettings(bucket="test-bucket"))


def key(n: int, path: str = "documents/d1/original") -> TenantScopedKey:
    return TenantScopedKey(tenant(n), path)


def test_put_get_roundtrip_and_immutability(store: ObjectStore) -> None:
    assert store.put(key(1), b"abc", content_type="text/plain").size == 3
    assert store.get(key(1)) == b"abc" and store.exists(key(1))
    with pytest.raises(ConflictError):
        store.put(key(1), b"changed", content_type="text/plain")
    store.put(key(1), b"changed", content_type="text/plain", overwrite=True)
    assert store.get(key(1)) == b"changed"


def test_tenants_cannot_address_each_other(store: ObjectStore) -> None:
    store.put(key(1), b"secret", content_type="text/plain")
    assert not store.exists(key(2))
    with pytest.raises(NotFoundError):
        store.get(key(2))


def test_delete_is_idempotent(store: ObjectStore) -> None:
    store.put(key(1), b"x", content_type="text/plain")
    store.delete(key(1))
    store.delete(key(1))
    assert not store.exists(key(1))


def test_presign_returns_url(store: ObjectStore) -> None:
    store.put(key(1), b"x", content_type="text/plain")
    assert store.presign_get(key(1), expires_seconds=60).startswith(("http", "memory://"))


@pytest.fixture(params=["memory", "redis"])
def limiter_factory(request: pytest.FixtureRequest) -> Callable[[FakeClock], RateLimiter]:
    if request.param == "memory":
        return InMemoryRateLimiter
    server = fakeredis.FakeServer()
    return lambda clock: RedisTokenBucket(fakeredis.FakeRedis(server=server), clock)


POLICY = RateLimitPolicy(capacity=2, refill_per_second=1.0)


def test_token_bucket_blocks_then_refills(
    limiter_factory: Callable[[FakeClock], RateLimiter],
) -> None:
    clock = FakeClock()
    limiter = limiter_factory(clock)
    k = RateLimitKey(tenant(1), "api:u")
    assert limiter.acquire(k, POLICY).allowed and limiter.acquire(k, POLICY).allowed
    denied = limiter.acquire(k, POLICY)
    assert not denied.allowed and denied.retry_after > timedelta(0)
    clock.advance(timedelta(seconds=1.1))
    assert limiter.acquire(k, POLICY).allowed


def test_buckets_are_tenant_isolated(limiter_factory: Callable[[FakeClock], RateLimiter]) -> None:
    limiter = limiter_factory(FakeClock())
    for _ in range(2):
        limiter.acquire(RateLimitKey(tenant(1), "api:u"), POLICY)
    assert limiter.acquire(RateLimitKey(tenant(2), "api:u"), POLICY).allowed


def test_fail_open_degrades_to_local_limiter() -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    class Broken:
        def acquire(self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1) -> object:
            raise RedisConnectionError("down")

    limiter = FailOpenRateLimiter(Broken(), InMemoryRateLimiter(FakeClock()))  # type: ignore[arg-type]
    k = RateLimitKey(tenant(1), "api:u")
    assert limiter.acquire(k, POLICY).allowed and limiter.acquire(k, POLICY).allowed
    assert not limiter.acquire(k, POLICY).allowed  # still limited per instance
