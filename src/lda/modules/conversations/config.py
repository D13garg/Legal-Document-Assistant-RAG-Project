"""The `conversations` configuration slice (ADR-031, ADR-039). Safe defaults; no credentials."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConversationsSettings(BaseModel):
    """`LDA_CONVERSATIONS__<FIELD>`. The upper bounds equal the database CHECK ceilings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_messages: int = Field(default=200, ge=1, le=1_000)
    max_text_chars: int = Field(default=8_000, ge=1, le=32_000)
    max_list: int = Field(default=50, ge=1, le=200)
