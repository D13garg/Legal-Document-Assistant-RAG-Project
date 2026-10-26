"""The `citations` configuration slice (ADR-031, ADR-016). Safe defaults; no credentials."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CitationsSettings(BaseModel):
    """`LDA_CITATIONS__<FIELD>`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_sources: int = Field(default=50, ge=1, le=500)
    max_markers: int = Field(default=200, ge=1, le=2_000)
