from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TypeVar

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from lda.shared.kernel.errors import ConfigurationError

S = TypeVar("S", bound=BaseModel)  # platform and module-owned slices (ADR-031, step 3)
PREFIX = "LDA_"


def _parse(value: str) -> object:
    """JSON-looking values (maps/lists) are parsed; others stay strings for lax coercion."""
    if value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class ConfigurationLoader:
    """Reads `LDA_<SECTION>__<FIELD>=value` variables and validates one slice at a time.

    Errors name the offending fields but never echo values (they may be secrets).
    """

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._sections: dict[str, dict[str, str]] = {}
        for name, value in environ.items():
            if not name.startswith(PREFIX) or "__" not in name:
                continue
            section, _, field = name[len(PREFIX) :].partition("__")
            if section and field:
                self._sections.setdefault(section.lower(), {})[field.lower()] = value

    @classmethod
    def from_environment(cls) -> ConfigurationLoader:
        return cls(os.environ)

    def load(self, section: str, model: type[S]) -> S:
        raw = self._sections.get(section.lower(), {})
        try:
            return model.model_validate({k: _parse(v) for k, v in raw.items()})
        except PydanticValidationError as exc:
            problems = sorted(
                {".".join(str(p) for p in e["loc"]) or "<section>" for e in exc.errors()}
            )
        # Raised outside the `except` block on purpose: inside it, `__context__` would keep the
        # pydantic error alive, and that error holds the offending input values (maybe secrets).
        raise ConfigurationError(
            "configuration_invalid",
            detail=f"section '{section}': invalid or missing fields: {', '.join(problems)}",
        )
