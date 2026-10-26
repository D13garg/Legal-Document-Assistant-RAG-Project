from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from lda.modules.access_control.domain.errors import InvalidGrant
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.shared.kernel.ids import PrincipalId, TenantId


@dataclass(frozen=True, slots=True)
class Grant:
    """One explicit permission: this principal may perform this action on this resource."""

    tenant_id: TenantId
    principal_id: PrincipalId
    resource: ResourceRef
    action: ActionId
    granted_at: datetime

    def __post_init__(self) -> None:
        if self.granted_at.tzinfo is None or self.granted_at.utcoffset() is None:
            raise InvalidGrant("grant_time_naive")
