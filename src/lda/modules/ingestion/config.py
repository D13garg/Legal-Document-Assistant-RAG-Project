"""The `ingestion` configuration slice (ADR-010 §11). Operator settings, not retry budgets."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IngestionSettings(BaseModel):
    """`LDA_INGESTION__WATCHDOG_DELAY_SECONDS` and `__MAX_OUTCOME_REDELIVERIES`.

    `watchdog_delay_seconds` must exceed the worker lease plus the backoff cap and the longest
    single stage attempt (bootstrap checks the first two): a run with no progress for this long is
    dead-lettered, and an outcome Documents has not acknowledged for this long is re-published.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    watchdog_delay_seconds: int = Field(default=1800, ge=60)
    max_outcome_redeliveries: int = Field(default=5, ge=0)
