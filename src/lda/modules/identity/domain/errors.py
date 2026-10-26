"""Identity domain errors. `detail` is always empty: principal data never enters an error."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError


class InvalidDisplayName(DomainError):
    def __init__(self) -> None:
        super().__init__("display_name_invalid")


class InvalidPrincipal(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class InvalidStatusTransition(DomainError):
    def __init__(self) -> None:
        super().__init__("principal_invalid_transition")
