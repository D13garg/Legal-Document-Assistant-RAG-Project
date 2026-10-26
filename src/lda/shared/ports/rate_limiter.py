from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from lda.shared.kernel.ids import TenantId


@dataclass(frozen=True, slots=True)
class RateLimitKey:
    """Always tenant-qualified so one tenant can never consume another's budget."""

    tenant_id: TenantId
    bucket: str  # e.g. "api:principal:<id>"

    def render(self) -> str:
        return f"rl:{self.tenant_id}:{self.bucket}"


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Token bucket: `capacity` burst, refilled at `refill_per_second`."""

    capacity: int
    refill_per_second: float

    def __post_init__(self) -> None:
        if self.capacity < 1 or self.refill_per_second <= 0:
            raise ValueError("capacity must be >= 1 and refill_per_second > 0")


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after: timedelta


class RateLimiter(Protocol):
    def acquire(
        self, key: RateLimitKey, policy: RateLimitPolicy, cost: int = 1
    ) -> RateDecision: ...
