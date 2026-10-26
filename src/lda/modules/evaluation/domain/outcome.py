"""What a system under test did with a question (ADR-018). `INSUFFICIENT_EVIDENCE` is first-class
(invariant 7).
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"  # observed only; a case can never EXPECT an error
