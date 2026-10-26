from __future__ import annotations

import threading
from datetime import timedelta

from lda.shared.ports.clock import Clock
from lda.shared.ports.rate_limiter import RateDecision, RateLimitKey, RateLimitPolicy


class InMemoryRateLimiter:
    """Per-process token bucket. The degraded-mode fallback and the test double."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)

    def acquire(self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1) -> RateDecision:
        if not 1 <= cost <= policy.capacity:
            raise ValueError("cost must be between 1 and the policy capacity")
        now = self._clock.now().timestamp()
        with self._lock:
            tokens, last = self._buckets.get(key.render(), (float(policy.capacity), now))
            tokens = min(
                float(policy.capacity), tokens + max(now - last, 0.0) * policy.refill_per_second
            )
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            self._buckets[key.render()] = (tokens, now)
        retry = 0.0 if allowed else (cost - tokens) / policy.refill_per_second
        return RateDecision(allowed, int(tokens), timedelta(seconds=retry))
