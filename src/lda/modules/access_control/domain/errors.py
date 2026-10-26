"""Access Control domain errors. Codes are stable; `detail` stays empty (no ids, no keys)."""

from __future__ import annotations

from lda.shared.kernel.errors import DomainError


class InvalidResourceRef(DomainError):
    def __init__(self) -> None:
        super().__init__("resource_ref_invalid")


class InvalidAction(DomainError):
    def __init__(self) -> None:
        super().__init__("action_invalid")


class InvalidGrant(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class InvalidScope(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class InvalidDecision(DomainError):
    def __init__(self) -> None:
        super().__init__("decision_invalid")


class InvalidRegistration(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class InvalidSigningKey(DomainError):
    def __init__(self, code: str) -> None:
        super().__init__(code)


class InvalidProofClaims(DomainError):
    def __init__(self) -> None:
        super().__init__("proof_claims_invalid")
