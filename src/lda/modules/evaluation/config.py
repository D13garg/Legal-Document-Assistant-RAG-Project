"""The `evaluation` configuration slice (ADR-031, ADR-018). Safe defaults; no credentials, no
paths.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EvaluationSettings(BaseModel):
    """`LDA_EVALUATION__<FIELD>`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cases: int = Field(default=1_000, ge=1, le=10_000)
    max_question_characters: int = Field(default=8_000, ge=1)
    max_evidence_age_seconds: int = Field(default=604_800, ge=60, le=2_592_000)
    """How long a stored evaluation run counts as release evidence (ADR-041 D4): 7 days by
    default, never more than 30."""
