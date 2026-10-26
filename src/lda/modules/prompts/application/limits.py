"""Size limits of the renderer, built once at composition from the config slice.

All character limits apply to the RENDERED text (wrapper and escaping included) and count Unicode
code points (`len(str)`), exactly as the gateway does: no normalization, so a combining mark is one
more character. Because escaping never shortens text, a raw length above a limit is rejected
before any escaping work.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PromptLimits:
    max_parts: int
    max_part_characters: int
    max_instruction_characters: int
    max_question_characters: int
    max_total_characters: int
    """Rendered instructions plus every rendered part (the gateway counts them together)."""

    def __post_init__(self) -> None:
        values = (
            self.max_parts,
            self.max_part_characters,
            self.max_instruction_characters,
            self.max_question_characters,
            self.max_total_characters,
        )
        if any(type(v) is not int or v < 1 for v in values):
            raise ValueError("limits must be positive integers")
        if self.max_question_characters > self.max_instruction_characters:
            raise ValueError("the question limit cannot exceed the instruction limit")
        if self.max_total_characters < self.max_instruction_characters:
            raise ValueError("the total limit cannot be below the instruction limit")
