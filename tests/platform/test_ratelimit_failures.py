"""Rate limiter validation, provider (Redis) failure behaviour and tenant isolation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta

import fakeredis
import pytest
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from lda.platform.configuration.settings import RateLimitSettings
from lda.platform.ratelimit.factory import create_rate_limiter
from lda.platform.ratelimit.fail_open import FailOpenRateLimiter
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.ratelimit.redis_bucket import RedisTokenBucket
from lda.platform.testing.doubles import FakeClock
from lda.shared.ports.rate_limiter import RateDecision, RateLimiter, RateLimitKey, RateLimitPolicy
from platform_support import tenant

POLICY = RateLimitPolicy(capacity=2, refill_per_second=1.0)


def k(n: int = 1, bucket: str = "api:u") -> RateLimitKey:
    return RateLimitKey(tenant(n), bucket)


@pytest.fixture(params=["memory", "redis"])
def make(request: pytest.FixtureRequest) -> Callable[[FakeClock], RateLimiter]:
    if request.param == "memory":
        return InMemoryRateLimiter
    server = fakeredis.FakeServer()
    return lambda clock: RedisTokenBucket(fakeredis.FakeRedis(server=server), clock)


# ---- input validation (contract for every adapter) -----------------------------------------------


@pytest.mark.parametrize("cost", [0, -1, -100, 3, 10**6])
def test_unsatisfiable_or_negative_costs_are_rejected_and_change_nothing(
    make: Callable[[FakeClock], RateLimiter], cost: int
) -> None:
    limiter = make(FakeClock())
    with pytest.raises(ValueError, match="cost"):
        limiter.acquire(k(), POLICY, cost)
    assert [limiter.acquire(k(), POLICY).allowed for _ in range(3)] == [True, True, False]


def test_a_negative_cost_cannot_be_used_to_mint_tokens(
    make: Callable[[FakeClock], RateLimiter],
) -> None:
    limiter = make(FakeClock())
    with pytest.raises(ValueError):
        limiter.acquire(k(), POLICY, -50)
    assert limiter.acquire(k(), POLICY).remaining == 1  # still the original burst budget


def test_cost_up_to_capacity_is_allowed_and_denial_reports_retry_after(
    make: Callable[[FakeClock], RateLimiter],
) -> None:
    limiter = make(FakeClock())
    assert limiter.acquire(k(), POLICY, 2).allowed
    denied = limiter.acquire(k(), POLICY, 2)
    assert not denied.allowed and denied.retry_after == timedelta(seconds=2)


# ---- fail-open on provider failure ---------------------------------------------------------------


def outage() -> tuple[fakeredis.FakeServer, FailOpenRateLimiter, FakeClock]:
    clock, server = FakeClock(), fakeredis.FakeServer()
    limiter = FailOpenRateLimiter(
        RedisTokenBucket(fakeredis.FakeRedis(server=server), clock), InMemoryRateLimiter(clock)
    )
    return server, limiter, clock


def test_a_real_redis_outage_degrades_to_per_instance_limits_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, limiter, _ = outage()
    assert limiter.acquire(k(), POLICY).allowed  # served by Redis
    server.connected = False
    with caplog.at_level(logging.WARNING, logger="lda.ratelimit"):
        decisions = [limiter.acquire(k(), POLICY).allowed for _ in range(3)]
    assert decisions == [True, True, False]  # not rejected outright, but still limited locally
    assert [r.getMessage() for r in caplog.records] == ["rate_limiter_degraded"] * 3
    server.connected = True
    assert isinstance(limiter.acquire(k(2), POLICY), RateDecision)
    assert fakeredis.FakeRedis(server=server).keys("rl:*") == [
        f"rl:{tenant(1)}:api:u".encode(),
        f"rl:{tenant(2)}:api:u".encode(),
    ]  # Redis serves again after recovery


@pytest.mark.parametrize(
    "error", [RedisConnectionError("x"), RedisTimeoutError("x"), TimeoutError(), OSError("x")]
)
def test_provider_errors_degrade(error: Exception) -> None:
    class Broken:
        def acquire(
            self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1
        ) -> RateDecision:
            raise error

    limiter = FailOpenRateLimiter(Broken(), InMemoryRateLimiter(FakeClock()))
    assert limiter.acquire(k(), POLICY).allowed


@pytest.mark.parametrize("error", [ValueError("bug"), TypeError("bug"), KeyError("bug")])
def test_programming_errors_are_not_swallowed_as_outages(error: Exception) -> None:
    class Buggy:
        def acquire(
            self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1
        ) -> RateDecision:
            raise error

    limiter = FailOpenRateLimiter(Buggy(), InMemoryRateLimiter(FakeClock()))
    with pytest.raises(type(error)):
        limiter.acquire(k(), POLICY)


def test_invalid_cost_is_a_caller_error_not_an_outage() -> None:
    _, limiter, _ = outage()
    with pytest.raises(ValueError, match="cost"):
        limiter.acquire(k(), POLICY, -1)  # must not be "recovered" by the fallback


def test_degraded_log_event_carries_no_tenant_or_key(caplog: pytest.LogCaptureFixture) -> None:
    server, limiter, _ = outage()
    server.connected = False
    with caplog.at_level(logging.WARNING, logger="lda.ratelimit"):
        limiter.acquire(k(bucket="api:principal:SECRET-PRINCIPAL"), POLICY)
    assert "SECRET-PRINCIPAL" not in caplog.text and str(tenant(1)) not in caplog.text


def test_fallback_keeps_tenants_isolated_during_an_outage() -> None:
    server, limiter, _ = outage()
    server.connected = False
    assert [limiter.acquire(k(1), POLICY).allowed for _ in range(3)] == [True, True, False]
    assert limiter.acquire(k(2), POLICY).allowed


# ---- redis bucket specifics ----------------------------------------------------------------------


def test_redis_keys_are_tenant_qualified_and_expire() -> None:
    server = fakeredis.FakeServer()
    client = fakeredis.FakeRedis(server=server)
    RedisTokenBucket(client, FakeClock()).acquire(k(3, "api:u"), POLICY)
    (only_key,) = client.keys("*")
    assert only_key == f"rl:{tenant(3)}:api:u".encode()
    assert (
        0 < client.pttl(only_key) <= 3000
    )  # capacity/refill + 1s: idle buckets are not kept forever


def test_factory_degrades_by_construction_without_redis_and_hides_the_url() -> None:
    clock = FakeClock()
    assert isinstance(create_rate_limiter(RateLimitSettings(), clock), InMemoryRateLimiter)
    settings = RateLimitSettings(redis_url=SecretStr("redis://:TOPSECRETPW@localhost:6399/0"))
    assert isinstance(create_rate_limiter(settings, clock), FailOpenRateLimiter)  # lazy: no connect
    assert "TOPSECRETPW" not in repr(settings) and "TOPSECRETPW" not in settings.model_dump_json()
