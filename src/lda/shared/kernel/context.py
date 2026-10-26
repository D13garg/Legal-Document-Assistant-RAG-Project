"""Execution contexts. Tenant scope is explicit and distinct for user work and system work."""

from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import JobId, PrincipalId, RequestId, TenantId


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Work performed on behalf of an authenticated principal inside one tenant."""

    tenant_id: TenantId
    principal_id: PrincipalId
    request_id: RequestId


@dataclass(frozen=True, slots=True)
class SystemContext:
    """Work performed by the platform (jobs, sweepers) inside one tenant.

    A distinct type on purpose: system work must never masquerade as a user action.
    """

    tenant_id: TenantId
    purpose: str
    job_id: JobId | None = None

    def __post_init__(self) -> None:
        if not self.purpose.strip():
            raise ValueError("SystemContext.purpose is required (it is recorded in audit)")


ExecutionContext = TenantContext | SystemContext
