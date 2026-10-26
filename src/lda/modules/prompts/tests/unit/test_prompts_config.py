"""The `prompts` configuration slice (ADR-031)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from lda.modules.prompts.config import PromptsSettings
from lda.modules.prompts.testing import default_limits


def test_defaults_match_the_testing_limits() -> None:
    s = PromptsSettings()
    limits = default_limits()
    assert (s.max_parts, s.max_part_characters, s.max_instruction_characters) == (
        limits.max_parts,
        limits.max_part_characters,
        limits.max_instruction_characters,
    )
    assert (s.max_question_characters, s.max_total_characters) == (
        limits.max_question_characters,
        limits.max_total_characters,
    )


def test_unknown_and_invalid_fields_are_refused() -> None:
    with pytest.raises(PydanticValidationError):
        PromptsSettings(unknown=1)  # type: ignore[call-arg]
    with pytest.raises(PydanticValidationError):
        PromptsSettings(max_parts=0)
    with pytest.raises(PydanticValidationError):
        PromptsSettings(max_parts=10_001)


def test_settings_are_frozen() -> None:
    with pytest.raises(PydanticValidationError):
        PromptsSettings().max_parts = 1
