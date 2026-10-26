"""Proof verification contract for the LLM egress boundary (ADR-026).

Only the contract lives here. `ai_gateway` depends on this port, never on `access_control`; the
composition root injects the concrete verifier. Values are neutral wire forms so that no Access
Control type crosses the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lda.shared.kernel.ids import PrincipalId, TenantId


@dataclass(frozen=True, slots=True)
class ProofResource:
    """Neutral `(kind, id)` form of an authorized resource."""

    kind: str
    id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProofExpectation:
    """What the caller is about to do; the proof must bind exactly this."""

    tenant_id: TenantId
    principal_id: PrincipalId
    action: str
    resources: frozenset[ProofResource]


class ProofVerifier(Protocol):
    def verify(self, token: str, expected: ProofExpectation) -> None:
        """Return only if `token` authorizes exactly `expected` right now.

        Raises `AuthorizationError` (stable `proof_*` codes) when the token is malformed, forged,
        for something else, expired or stale. Any other exception (for example a database failure
        while checking the live policy epoch) also means: do not send anything to the provider.
        There is no "soft" result: callers must not proceed unless this returns.
        """
        ...
