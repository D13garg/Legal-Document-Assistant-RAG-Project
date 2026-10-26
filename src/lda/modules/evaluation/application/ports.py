"""The system-under-test port (ADR-018 section 4)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.evaluation.domain.dataset import EvaluationCase
from lda.modules.evaluation.domain.observation import Observation


class EvaluationTarget(Protocol):
    """Runs one case against the system being evaluated and reports what it did.

    Contract:
    - it returns an `Observation` (identifiers and outcome only) or raises; a raise is
    recorded as a case
      error, never retried, and its message is discarded;
    - it must run the real authorization path as a dedicated evaluation principal (a later
    step adds the
      adapter); the core never builds a scope, proof or context and never sees document text;
    - it does not log, cache or persist the question.
    """

    def observe(self, case: EvaluationCase) -> Observation: ...
