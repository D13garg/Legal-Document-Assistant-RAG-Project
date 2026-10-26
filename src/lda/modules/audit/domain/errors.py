"""Audit domain errors. `detail` is always empty: a rejected fact is never echoed."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError


class InvalidAuditEntry(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
