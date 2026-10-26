"""Shared validation of the small, content-free vocabulary the evaluation domain uses (ADR-018)."""

from __future__ import annotations

import math
import re

from lda.modules.evaluation.domain.codes import INVALID_INPUT
from lda.shared.kernel.errors import ValidationError

MAX_LABEL_LENGTH = 128
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#/-]*")


def label(value: object, field: str) -> str:
    """An opaque identifier (case, source, metric, component or parameter name). Never free text."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_LABEL_LENGTH
        or _LABEL.fullmatch(value) is None
    ):
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


def unit_interval(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError(INVALID_INPUT, detail=field)
    number = float(value)
    if math.isnan(number) or not 0.0 <= number <= 1.0:
        raise ValidationError(INVALID_INPUT, detail=field)
    return number


def positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError(INVALID_INPUT, detail=field)
    return value


def label_set(values: object, field: str, *, max_items: int) -> tuple[str, ...]:
    """A duplicate-free collection of labels, returned sorted so equal sets are equal values."""
    if not isinstance(values, tuple | list | frozenset | set):
        raise ValidationError(INVALID_INPUT, detail=field)
    checked = [label(v, field) for v in values]
    if len(set(checked)) != len(checked) or len(checked) > max_items:
        raise ValidationError(INVALID_INPUT, detail=field)
    return tuple(sorted(checked))
