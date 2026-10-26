"""Immutable, code-defined, versioned prompt templates and the catalog that selects them (ADR-017).

There is no database, no activation state and no runtime editing: a template exists because it is
in the source tree. Selection is always explicit (`template_id`, `version`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lda.modules.prompts.domain import codes
from lda.modules.prompts.domain.escaping import contains_marker
from lda.shared.kernel.errors import ValidationError

TEMPLATE_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
MAX_TEMPLATE_VERSION = 1_000_000


def is_template_id(value: object) -> bool:
    return isinstance(value, str) and TEMPLATE_ID_PATTERN.fullmatch(value) is not None


def is_template_version(value: object) -> bool:
    return type(value) is int and 1 <= value <= MAX_TEMPLATE_VERSION


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A trusted template. `instructions` is trusted text written by the maintainers; it is rendered
    before the untrusted question block. `test_only` templates never enter a production catalog."""

    template_id: str
    version: int
    instructions: str
    test_only: bool = False

    def __post_init__(self) -> None:
        if not is_template_id(self.template_id):
            raise ValueError("invalid template_id")
        if not is_template_version(self.version):
            raise ValueError("invalid template version")
        if type(self.instructions) is not str or not self.instructions.strip():
            raise ValueError("template instructions must be non-empty text")
        if contains_marker(self.instructions):
            raise ValueError("template instructions must not contain a structural marker")
        if type(self.test_only) is not bool:
            raise ValueError("test_only must be a bool")


@dataclass(frozen=True, slots=True)
class PromptCatalog:
    """An immutable set of templates keyed by `(template_id, version)`. Lookup is a linear scan over
    a tuple: deterministic, no hidden state, no cache."""

    templates: tuple[PromptTemplate, ...]
    production: bool = True

    def __post_init__(self) -> None:
        if type(self.templates) is not tuple or not all(
            isinstance(t, PromptTemplate) for t in self.templates
        ):
            raise ValueError("templates must be a tuple of PromptTemplate")
        keys = [(t.template_id, t.version) for t in self.templates]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate template id and version")
        if self.production and any(t.test_only for t in self.templates):
            raise ValueError("a production catalog cannot contain a test-only template")

    def get(self, template_id: object, version: object) -> PromptTemplate:
        """The exact template, or `prompt_template_unknown`. The error never echoes the input."""
        if type(template_id) is str and type(version) is int:  # `True == 1`: exact types only
            for template in self.templates:
                if template.template_id == template_id and template.version == version:
                    return template
        raise ValidationError(codes.TEMPLATE_UNKNOWN)
