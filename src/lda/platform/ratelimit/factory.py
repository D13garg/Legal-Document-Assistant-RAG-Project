from __future__ import annotations

import redis

from lda.platform.configuration.settings import RateLimitSettings
from lda.platform.ratelimit.fail_open import FailOpenRateLimiter
from lda.platform.ratelimit.memory import InMemoryRateLimiter
from lda.platform.ratelimit.redis_bucket import RedisTokenBucket
from lda.shared.ports.clock import Clock
from lda.shared.ports.rate_limiter import RateLimiter


def create_rate_limiter(settings: RateLimitSettings, clock: Clock) -> RateLimiter:
    local = InMemoryRateLimiter(clock)
    if settings.redis_url is None:
        return local
    client = redis.Redis.from_url(
        settings.redis_url.get_secret_value(), socket_timeout=settings.socket_timeout_seconds
    )
    return FailOpenRateLimiter(RedisTokenBucket(client, clock), local)
