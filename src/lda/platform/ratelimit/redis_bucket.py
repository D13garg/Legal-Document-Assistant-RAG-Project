from __future__ import annotations

import typing
from datetime import timedelta

from lda.shared.ports.clock import Clock
from lda.shared.ports.rate_limiter import RateDecision, RateLimitKey, RateLimitPolicy

_SCRIPT = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + math.max(now - ts, 0) * refill)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / refill * 1000) + 1000)
return {allowed, tostring(tokens)}
"""


class RedisTokenBucket:
    """Distributed token bucket (atomic Lua). Time comes from the injected Clock."""

    def __init__(self, client: typing.Any, clock: Clock) -> None:
        self._script = client.register_script(_SCRIPT)
        self._clock = clock

    def acquire(self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1) -> RateDecision:
        if not 1 <= cost <= policy.capacity:
            raise ValueError("cost must be between 1 and the policy capacity")
        now = self._clock.now().timestamp()
        allowed, tokens = self._script(
            keys=[key.render()], args=[policy.capacity, policy.refill_per_second, cost, now]
        )
        remaining = float(tokens)
        retry = 0.0 if allowed else (cost - remaining) / policy.refill_per_second
        return RateDecision(bool(allowed), int(remaining), timedelta(seconds=retry))
