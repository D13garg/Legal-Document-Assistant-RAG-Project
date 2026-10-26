from __future__ import annotations

import re

from lda.shared.kernel.errors import DomainError

ERROR_CODE_PATTERN = r"[a-z][a-z0-9_.]{0,63}"
_ERROR_CODE = re.compile(ERROR_CODE_PATTERN)


class InvalidTransition(DomainError):
    """A run or stage was asked to leave a state it cannot leave that way."""

    def __init__(self) -> None:
        super().__init__("ingestion_transition_invalid")


class InvalidRun(DomainError):
    """A run was built from values that break its invariants."""

    def __init__(self, code: str) -> None:
        super().__init__(code)


def valid_error_code(code: str) -> str:
    """Failure codes are identifiers, never free text (they reach logs, events and metrics)."""
    if not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None:
        raise InvalidRun("ingestion_error_code_invalid")
    return code
