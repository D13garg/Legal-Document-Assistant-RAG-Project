"""Bounds on one evaluation run (ADR-018). Checked before the first case runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvaluationLimits:
    max_cases: int
    max_question_characters: int
