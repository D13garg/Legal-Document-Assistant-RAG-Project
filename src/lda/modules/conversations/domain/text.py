"""The text of a user message: untrusted, possibly confidential, never shown in a repr or error."""

from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.conversations.domain import codes
from lda.shared.kernel.errors import DomainError

HARD_MAX_TEXT_CHARS = 32_000
"""Absolute ceiling, equal to the database CHECK. Configuration may only lower it."""


@dataclass(frozen=True, slots=True)
class MessageText:
    """Non-blank text without NUL characters, encodable as UTF-8, within the ceiling.

    The text is stored exactly as submitted (no trimming or normalization): it is the user's
    words. `repr=False` keeps it out of every representation.
    """

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        value = self.value
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > HARD_MAX_TEXT_CHARS
            or "\x00" in value
        ):
            raise DomainError(codes.MESSAGE_INVALID)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise DomainError(codes.MESSAGE_INVALID) from None

    @classmethod
    def within(cls, raw: str, max_chars: int) -> MessageText:
        """Build the text, refusing anything longer than the configured limit."""
        if not isinstance(raw, str) or len(raw) > min(max_chars, HARD_MAX_TEXT_CHARS):
            raise DomainError(codes.MESSAGE_INVALID)
        return cls(raw)
