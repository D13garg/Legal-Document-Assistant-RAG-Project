"""The `retrieval` configuration slice (ADR-031, ADR-008). Safe defaults throughout."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RetrievalSettings(BaseModel):
    """`LDA_RETRIEVAL__MAX_RESULTS`, `__MAX_QUERY_CHARACTERS`, `__CANDIDATE_POOL`,
    `__MAX_CANDIDATES` and `__EMBEDDING_BATCH_SIZE`.

    The candidate pool is how many nearest rows one page reads before the live eligibility check;
    `max_candidates` bounds how many rows one search may inspect in total (so a search through a
    scope full of superseded versions ends, with fewer results, instead of scanning forever).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_results: int = Field(default=20, ge=1, le=100)
    max_query_characters: int = Field(default=2000, ge=1, le=20_000)
    candidate_pool: int = Field(default=200, ge=1, le=2000)
    max_candidates: int = Field(default=2000, ge=1, le=20_000)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
