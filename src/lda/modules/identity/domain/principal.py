from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from lda.modules.identity.domain.display_name import DisplayName
from lda.modules.identity.domain.errors import InvalidPrincipal, InvalidStatusTransition
from lda.shared.kernel.ids import PrincipalId, TenantId


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE = "service"


class PrincipalStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class Principal:
    """WHO an actor is, inside exactly one tenant. Carries no permissions and no credentials.

    Immutable: `id`, `tenant_id`, `kind` and `created_at` never change; lifecycle moves return a
    new value with a higher `version` (used for optimistic concurrency by the repository).
    """

    id: PrincipalId
    tenant_id: TenantId
    kind: PrincipalKind
    display_name: DisplayName
    status: PrincipalStatus
    version: int
    created_at: datetime
    status_changed_at: datetime

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvalidPrincipal("principal_version_invalid")
        for moment in (self.created_at, self.status_changed_at):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise InvalidPrincipal("principal_time_naive")
        if self.status_changed_at < self.created_at:
            raise InvalidPrincipal("principal_time_order_invalid")

    @classmethod
    def register(
        cls,
        *,
        id: PrincipalId,
        tenant_id: TenantId,
        kind: PrincipalKind,
        display_name: DisplayName,
        now: datetime,
    ) -> Principal:
        return cls(
            id=id,
            tenant_id=tenant_id,
            kind=kind,
            display_name=display_name,
            status=PrincipalStatus.ACTIVE,
            version=1,
            created_at=now,
            status_changed_at=now,
        )

    @property
    def is_active(self) -> bool:
        return self.status is PrincipalStatus.ACTIVE

    def disable(self, now: datetime) -> Principal:
        return self._move(PrincipalStatus.ACTIVE, PrincipalStatus.DISABLED, now)

    def enable(self, now: datetime) -> Principal:
        return self._move(PrincipalStatus.DISABLED, PrincipalStatus.ACTIVE, now)

    def _move(self, expected: PrincipalStatus, target: PrincipalStatus, now: datetime) -> Principal:
        if self.status is not expected:
            raise InvalidStatusTransition
        return replace(self, status=target, version=self.version + 1, status_changed_at=now)
