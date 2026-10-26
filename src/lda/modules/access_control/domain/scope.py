"""AccessScope: the frozen result of an authorization decision (ADR-014 §7)."""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.errors import InvalidScope
from lda.modules.access_control.domain.resource import ResourceRef
from lda.shared.kernel.ids import PrincipalId, TenantId

MAX_SCOPE_RESOURCES = 10_000
"""v1 guard on request/scope size (bounds query parameters and work); ADR-008 revisits it."""


@dataclass(frozen=True, slots=True, repr=False)
class AccessScope:
    """Tenant, principal, policy epoch and the EXACT set of authorized resources.

    Not a permission cache: nothing widens or re-resolves it. Only Access Control creates one
    (AG090); consumers receive it from a decision and use it to build or filter context.
    """

    tenant_id: TenantId
    principal_id: PrincipalId
    policy_epoch: PolicyEpoch
    resources: frozenset[ResourceRef]

    def __post_init__(self) -> None:
        if not isinstance(self.resources, frozenset) or not all(
            isinstance(r, ResourceRef) for r in self.resources
        ):
            raise InvalidScope("scope_resources_invalid")
        if len(self.resources) > MAX_SCOPE_RESOURCES:
            raise InvalidScope("scope_too_large")

    def __repr__(self) -> str:  # resource ids stay out of logs
        return (
            f"AccessScope(tenant_id={self.tenant_id}, principal_id={self.principal_id}, "
            f"policy_epoch={self.policy_epoch.value}, resources={len(self.resources)})"
        )
