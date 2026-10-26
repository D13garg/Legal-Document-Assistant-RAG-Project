"""The `grounded_generation` configuration slice (ADR-031, ADR-029). No credentials.

`model` has no default on purpose: choosing a provider model is a deployment decision, and a
missing value must fail closed when the slice is loaded rather than send traffic to a guess.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class GroundedGenerationSettings(BaseModel):
    """`LDA_GROUNDED_GENERATION__<FIELD>`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    template_id: str = Field(default="grounded_answer", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    template_version: int = Field(default=1, ge=1, le=1_000_000)
    max_output_tokens: int = Field(default=1_024, ge=1, le=32_768)
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_sources: int = Field(default=20, ge=1, le=500)
    max_output_characters: int = Field(default=24_000, ge=1, le=200_000)
    max_statements: int = Field(default=20, ge=1, le=200)
    max_statement_characters: int = Field(default=1_500, ge=1, le=20_000)
    min_quote_characters: int = Field(default=12, ge=1, le=1_000)
    max_quote_characters: int = Field(default=400, ge=1, le=5_000)
