from __future__ import annotations

from dataclasses import dataclass

from lda.modules.access_control.domain.errors import InvalidScope


@dataclass(frozen=True, slots=True, order=True)
class PolicyEpoch:
    """Monotonic per-tenant counter; 0 means no policy change yet (ADR-014 §8)."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 0:
            raise InvalidScope("policy_epoch_invalid")

    def next(self) -> PolicyEpoch:
        return PolicyEpoch(self.value + 1)
