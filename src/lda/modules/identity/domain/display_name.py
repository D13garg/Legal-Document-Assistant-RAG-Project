from __future__ import annotations

from dataclasses import dataclass

from lda.modules.identity.domain.errors import InvalidDisplayName

MAX_DISPLAY_NAME_LENGTH = 200


@dataclass(frozen=True, slots=True)
class DisplayName:
    """A human label for a principal. Untrusted input: trimmed, single-line, bounded.

    It is personal data: never log it and never put it in an error.
    """

    value: str

    def __post_init__(self) -> None:
        trimmed = self.value.strip()
        if not trimmed or len(trimmed) > MAX_DISPLAY_NAME_LENGTH or not trimmed.isprintable():
            raise InvalidDisplayName
        object.__setattr__(self, "value", trimmed)

    def __repr__(self) -> str:
        return "DisplayName(***)"
