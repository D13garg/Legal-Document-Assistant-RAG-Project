"""The `evaluation` configuration slice (ADR-031)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from lda.modules.evaluation.config import EvaluationSettings
from lda.modules.evaluation.testing import default_limits


def test_defaults_match_the_testing_limits() -> None:
    s, limits = EvaluationSettings(), default_limits()
    assert (s.max_cases, s.max_question_characters) == (
        limits.max_cases,
        limits.max_question_characters,
    )


def test_unknown_and_invalid_fields_are_refused_and_settings_are_frozen() -> None:
    for bad in (
        {"unknown": 1},
        {"max_cases": 0},
        {"max_cases": 10_001},
        {"max_question_characters": 0},
    ):
        with pytest.raises(PydanticValidationError):
            EvaluationSettings(**bad)
    with pytest.raises(PydanticValidationError):
        EvaluationSettings().max_cases = 1
