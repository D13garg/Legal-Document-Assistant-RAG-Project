"""The `answering` configuration slice (ADR-031, ADR-040). Safe defaults; no credentials.

`config_id` is OPTIONAL since Step 17 (ADR-041 D2). The effective configuration identity is
computed at start-up from the real settings (`bootstrap/rag_configuration.py`) and recorded on
every generation. A declared `config_id` is only an assertion: it must equal the computed value or
start-up fails. Its shape is validated here; shape alone proves nothing.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AnsweringSettings(BaseModel):
    """`LDA_ANSWERING__<FIELD>`. Upper bounds equal the ceilings the collaborators enforce."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str | None = Field(default=None, pattern=r"^rag-[0-9a-f]{64}$")
    max_candidates: int = Field(default=100, ge=1, le=10_000)
    max_question_characters: int = Field(default=2_000, ge=1, le=20_000)
    max_sources: int = Field(default=8, ge=1, le=100)
