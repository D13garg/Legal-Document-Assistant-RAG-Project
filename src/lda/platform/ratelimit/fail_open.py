from __future__ import annotations

import logging

from redis.exceptions import RedisError

from lda.shared.ports.rate_limiter import RateDecision, RateLimiter, RateLimitKey, RateLimitPolicy

log = logging.getLogger("lda.ratelimit")


class FailOpenRateLimiter:
    """Use the primary limiter; if it is unavailable, degrade to the per-instance fallback.

    "Fail open" means requests are not rejected because Redis is down, yet they are still limited
    per instance. Every degradation is logged.
    """

    def __init__(self, primary: RateLimiter, fallback: RateLimiter) -> None:
        self._primary = primary
        self._fallback = fallback

    def acquire(self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1) -> RateDecision:
        try:
            return self._primary.acquire(key, policy, cost)
        except (RedisError, OSError):
            log.warning("rate_limiter_degraded", extra={"fields": {"operation": "acquire"}})
            return self._fallback.acquire(key, policy, cost)
