from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from lda.modules.identity.domain.display_name import DisplayName
from lda.modules.identity.domain.principal import Principal, PrincipalKind
from lda.shared.kernel.ids import PrincipalId, TenantId


def a_principal(
    *,
    tenant_id: TenantId,
    id: PrincipalId | None = None,
    kind: PrincipalKind = PrincipalKind.USER,
    display_name: str = "Test Principal",
    now: datetime | None = None,
) -> Principal:
    """A freshly registered (ACTIVE) principal for tests."""
    return Principal.register(
        id=id or PrincipalId(UUID(int=0xB0B)),
        tenant_id=tenant_id,
        kind=kind,
        display_name=DisplayName(display_name),
        now=now or datetime(2026, 1, 1, tzinfo=UTC),
    )
