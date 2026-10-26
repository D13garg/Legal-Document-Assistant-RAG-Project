from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lda.modules.identity.domain.principal import Principal, PrincipalKind, PrincipalStatus
from lda.shared.kernel.ids import PrincipalId, TenantId


@dataclass(frozen=True, slots=True)
class RegisterPrincipalCommand:
    kind: PrincipalKind
    display_name: str  # untrusted; validated by the domain


@dataclass(frozen=True, slots=True)
class PrincipalReference:
    principal_id: PrincipalId


@dataclass(frozen=True, slots=True)
class PrincipalView:
    """What crosses the module boundary: identity facts only, never persistence objects."""

    principal_id: PrincipalId
    tenant_id: TenantId
    kind: PrincipalKind
    display_name: str
    status: PrincipalStatus
    created_at: datetime
    status_changed_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status is PrincipalStatus.ACTIVE

    def __repr__(self) -> str:  # the display name is personal data
        return (
            f"PrincipalView(principal_id={self.principal_id}, kind={self.kind.value}, "
            f"status={self.status.value})"
        )


def view_of(principal: Principal) -> PrincipalView:
    return PrincipalView(
        principal_id=principal.id,
        tenant_id=principal.tenant_id,
        kind=principal.kind,
        display_name=principal.display_name.value,
        status=principal.status,
        created_at=principal.created_at,
        status_changed_at=principal.status_changed_at,
    )
