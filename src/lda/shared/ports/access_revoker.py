"""Revoking access to a resource on the CALLER's transaction (ADR-023).

The owning module of a resource (for example `documents`) must not write Access Control's tables
(ADR-027), yet deleting a resource and losing access to it must be one atomic change. Access Control
implements this port over the caller's own transaction; bootstrap hands it to the owning module's
unit of work, the same pattern as `AuditRecorder`.
"""

from __future__ import annotations

from typing import Protocol


class ResourceAccessRevoker(Protocol):
    """Bound to one tenant's transaction for its whole life."""

    def revoke_all(self, resource_kind: str, resource_id: str) -> int:
        """Remove every grant on the resource and, if any was removed, advance the tenant's policy
        epoch exactly once, all inside the caller's transaction. Returns how many grants went.
        Commits nothing; a failure must fail the caller's action (fail-closed)."""
        ...
