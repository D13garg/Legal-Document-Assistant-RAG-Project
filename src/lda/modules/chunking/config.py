"""The `chunking` configuration slice (ADR-031, ADR-038). Safe defaults throughout."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ChunkingSettings(BaseModel):
    """`LDA_CHUNKING__MAX_CHARACTERS`, `__MIN_CHARACTERS` and `__MAX_CHUNKS`.

    The values shape the output and are recorded with every chunk set (inside the chunker
    version): changing them never rewrites a set that already exists, it only affects versions
    chunked afterwards. Cross-field rules (minimum at most half of the maximum) are enforced by
    the domain when the stage is composed, so a bad pair fails at startup.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_characters: int = Field(default=2000, ge=1)
    min_characters: int = Field(default=200, ge=1)
    max_chunks: int = Field(default=50_000, ge=1)
