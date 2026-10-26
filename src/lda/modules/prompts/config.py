"""The `prompts` configuration slice (ADR-031, ADR-017). Safe defaults throughout.

Every limit counts RENDERED characters and must not exceed the matching `ai_gateway` limit;
bootstrap refuses a combination the gateway would reject (`check_gateway_compatibility`).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PromptsSettings(BaseModel):
    """`LDA_PROMPTS__<FIELD>`. No credentials, no paths, no template text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_parts: int = Field(default=500, ge=1, le=10_000)
    max_part_characters: int = Field(default=50_000, ge=1)
    max_instruction_characters: int = Field(default=20_000, ge=1)
    max_question_characters: int = Field(default=8_000, ge=1)
    max_total_characters: int = Field(default=400_000, ge=1)
