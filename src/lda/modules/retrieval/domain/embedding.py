"""Embedding identity and vector rules (ADR-007).

An index is identified by everything that makes two vectors comparable: provider, model, model
version, dimensions, metric and normalization. Vectors of different identities are never compared,
and a change of identity is a new index that must be rebuilt, never an in-place rewrite.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass

from lda.shared.kernel.errors import ValidationError

EMBEDDING_DIMENSIONS = 256
"""Fixed by the schema column `vector(256)` (ADR-004). Another size is a new migration."""
METRIC = "cosine"
NORMALIZATION = "l2"
NORM_TOLERANCE = 1e-3

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
INDEX_ID_PATTERN = re.compile(r"[0-9a-f]{32}")

EMBEDDING_IDENTITY_INVALID = "embedding_identity_invalid"
EMBEDDING_VECTOR_INVALID = "embedding_vector_invalid"

Vector = tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingIdentity:
    provider: str
    model: str
    version: str
    dimensions: int = EMBEDDING_DIMENSIONS

    def __post_init__(self) -> None:
        for part in (self.provider, self.model, self.version):
            if not isinstance(part, str) or _NAME.fullmatch(part) is None:
                raise ValidationError(EMBEDDING_IDENTITY_INVALID)
        if self.dimensions != EMBEDDING_DIMENSIONS or isinstance(self.dimensions, bool):
            raise ValidationError(EMBEDDING_IDENTITY_INVALID)

    @property
    def index_id(self) -> str:
        """Deterministic and content-free: equal identities give equal ids, any difference in any
        part gives another one."""
        name = "/".join(
            (self.provider, self.model, self.version, str(self.dimensions), METRIC, NORMALIZATION)
        )
        return hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]


def checked_vector(values: Sequence[float]) -> Vector:
    """A finite, unit-length vector of exactly `EMBEDDING_DIMENSIONS` numbers, or a refusal."""
    if isinstance(values, str | bytes) or len(values) != EMBEDDING_DIMENSIONS:
        raise ValidationError(EMBEDDING_VECTOR_INVALID)
    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValidationError(EMBEDDING_VECTOR_INVALID)
        number = float(value)
        if not math.isfinite(number):
            raise ValidationError(EMBEDDING_VECTOR_INVALID)
        numbers.append(number)
    norm = math.sqrt(math.fsum(n * n for n in numbers))
    if abs(norm - 1.0) > NORM_TOLERANCE:
        raise ValidationError(EMBEDDING_VECTOR_INVALID)
    return tuple(numbers)


def to_literal(vector: Vector) -> str:
    """pgvector's text form. Numbers only: nothing here can carry text."""
    return "[" + ",".join(repr(v) for v in vector) + "]"
