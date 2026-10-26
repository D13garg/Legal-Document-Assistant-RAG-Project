"""Search bounds, built from the `retrieval` configuration slice by the composition root."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchLimits:
    max_results: int
    max_query_characters: int
    candidate_pool: int
    max_candidates: int

    def __post_init__(self) -> None:
        if min(self.max_results, self.max_query_characters, self.candidate_pool) < 1:
            raise ValueError("search limits must be positive")
        if self.max_candidates < self.candidate_pool:
            raise ValueError("max_candidates must be at least the candidate pool")
