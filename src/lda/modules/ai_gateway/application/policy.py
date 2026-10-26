"""Operating limits of the egress use case, built once at composition from the config slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from lda.shared.ports.rate_limiter import RateLimitPolicy


@dataclass(frozen=True, slots=True)
class EgressPolicy:
    provider_timeout: timedelta
    max_attempts: int
    backoff: timedelta
    backoff_cap: timedelta
    rate: RateLimitPolicy
    max_segments: int
    max_segment_characters: int
    max_request_characters: int
    max_instruction_characters: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.provider_timeout <= timedelta(0):
            raise ValueError("provider_timeout must be positive")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.backoff < timedelta(0) or self.backoff_cap < self.backoff:
            raise ValueError("backoff must be non-negative and not above its cap")
        limits = (
            self.max_segments,
            self.max_segment_characters,
            self.max_request_characters,
            self.max_instruction_characters,
            self.max_output_tokens,
        )
        if any(limit < 1 for limit in limits):
            raise ValueError("limits must be positive")

    def delay_before_retry(self, failed_attempts: int) -> timedelta:
        """Deterministic exponential backoff (no jitter), capped."""
        delay: timedelta = self.backoff * (2 ** (failed_attempts - 1))
        return min(delay, self.backoff_cap)
