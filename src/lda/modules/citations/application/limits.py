"""Bounds of the citations application layer (ADR-016)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CitationLimits:
    max_sources: int
    max_markers: int

    def __post_init__(self) -> None:
        if self.max_sources < 1 or self.max_markers < 1:
            raise ValueError("citation limits must be positive")
