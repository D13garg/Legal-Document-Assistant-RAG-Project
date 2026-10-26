"""The `ai_gateway` configuration slice (ADR-031, ADR-006). Safe defaults throughout."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AiGatewaySettings(BaseModel):
    """`LDA_AI_GATEWAY__<FIELD>`. No credentials live here: adapters (a later step) bring their own
    slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_attempts: int = Field(default=3, ge=1, le=5)
    """Total provider calls per request; only pre-response transport failures are retried."""
    backoff_seconds: float = Field(default=0.2, ge=0, le=10)
    backoff_cap_seconds: float = Field(default=2.0, ge=0, le=30)
    rate_capacity: int = Field(default=60, ge=1)
    rate_refill_per_second: float = Field(default=1.0, gt=0)
    max_segments: int = Field(default=500, ge=1, le=10_000)
    max_segment_characters: int = Field(default=50_000, ge=1)
    max_request_characters: int = Field(default=400_000, ge=1)
    max_instruction_characters: int = Field(default=20_000, ge=1)
    max_output_tokens: int = Field(default=4096, ge=1, le=32_768)
