"""Resource and action identities (ADR-014). Opaque to Access Control: shapes only."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lda.modules.access_control.domain.errors import InvalidAction, InvalidResourceRef

KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
RESOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Same shape as the ADR-034 declaration identifiers: lowercase dotted, at most 128 characters.
ACTION_PATTERN = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
MAX_ACTION_LENGTH = 128


def is_valid_kind(value: object) -> bool:
    return isinstance(value, str) and KIND_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True, slots=True, order=True)
class ActionId:
    """A dotted action identifier such as `documents.read_document`."""

    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, str)
            or len(self.value) > MAX_ACTION_LENGTH
            or ACTION_PATTERN.fullmatch(self.value) is None
        ):
            raise InvalidAction

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class ResourceRef:
    """`(kind, id)`: what a grant or a scope refers to. The id is owned by the owning module."""

    kind: str
    id: str

    def __post_init__(self) -> None:
        if (
            not is_valid_kind(self.kind)
            or not isinstance(self.id, str)
            or RESOURCE_ID_PATTERN.fullmatch(self.id) is None
        ):
            raise InvalidResourceRef
