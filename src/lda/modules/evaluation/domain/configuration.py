"""`RagConfiguration`: the identity of a retrieval/generation setup under evaluation (ADR-030,
invariant 15).

Evaluation cannot import `retrieval`, `chunking` or `grounded_generation` (the DAG), so a
configuration is
an opaque, validated description: named components (for example `retrieval`, `prompt`,
`generation`) each
holding scalar parameters. The composition layer fills it from the real settings. Any change to
any value
yields a different `config_id`, so a result can never be attributed to a configuration it was
not run on.
Nothing here is a secret or content: names are labels, values are short scalars; credentials
never belong.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass

from lda.modules.evaluation.domain.codes import INVALID_INPUT
from lda.modules.evaluation.domain.values import MAX_LABEL_LENGTH, label
from lda.shared.kernel.errors import ValidationError

SCHEMA = "evaluation.rag_configuration.v1"
MAX_COMPONENTS = 32
MAX_PARAMETERS = 64
Scalar = str | int | float | bool


def _scalar(value: object) -> Scalar:
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str) and value.isprintable() and 0 < len(value) <= MAX_LABEL_LENGTH:
        return value
    raise ValidationError(INVALID_INPUT, detail="parameter_value")


@dataclass(frozen=True, slots=True)
class ConfigComponent:
    name: str
    parameters: tuple[tuple[str, Scalar], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", label(self.name, "component"))
        keys = [label(k, "parameter") for k, _ in self.parameters]
        if not keys or len(keys) > MAX_PARAMETERS or keys != sorted(set(keys)):
            raise ValidationError(INVALID_INPUT, detail="parameters")
        for _, value in self.parameters:
            _scalar(value)


@dataclass(frozen=True, slots=True)
class RagConfiguration:
    components: tuple[ConfigComponent, ...]

    def __post_init__(self) -> None:
        names = [c.name for c in self.components]
        if not names or len(names) > MAX_COMPONENTS or names != sorted(set(names)):
            raise ValidationError(INVALID_INPUT, detail="components")

    @classmethod
    def create(cls, components: Mapping[str, Mapping[str, Scalar]]) -> RagConfiguration:
        if not isinstance(components, Mapping):
            raise ValidationError(INVALID_INPUT, detail="components")
        built = []
        for name, params in components.items():
            if not isinstance(params, Mapping):
                raise ValidationError(INVALID_INPUT, detail="parameters")
            built.append(ConfigComponent(name, tuple(sorted(params.items()))))
        return cls(tuple(sorted(built, key=lambda c: c.name)))

    @property
    def config_id(self) -> str:
        """`rag-` plus the SHA-256 of the canonical form; equal configurations have equal ids."""
        canonical = json.dumps(
            {
                "schema": SCHEMA,
                "components": [[c.name, [list(p) for p in c.parameters]] for c in self.components],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return "rag-" + hashlib.sha256(canonical.encode("ascii")).hexdigest()
