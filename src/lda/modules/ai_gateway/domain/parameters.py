"""Approved generation parameters. There is deliberately no field that could carry tools."""

from __future__ import annotations

import math
from dataclasses import dataclass

MAX_OUTPUT_TOKENS_CEILING = 32_768


@dataclass(frozen=True, slots=True)
class GenerationParameters:
    max_output_tokens: int = 1024
    temperature: float = 0.0

    def __post_init__(self) -> None:
        tokens, temperature = self.max_output_tokens, self.temperature
        if type(tokens) is not int or not 1 <= tokens <= MAX_OUTPUT_TOKENS_CEILING:
            raise ValueError("max_output_tokens must be an int between 1 and the ceiling")
        if type(temperature) not in (int, float) or not math.isfinite(temperature):
            raise ValueError("temperature must be a finite number")
        if not 0.0 <= temperature <= 1.0:
            raise ValueError("temperature must be between 0 and 1")
        object.__setattr__(self, "temperature", float(temperature))
