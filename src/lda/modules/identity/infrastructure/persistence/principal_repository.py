from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from lda.modules.identity.domain.display_name import DisplayName
from lda.modules.identity.domain.principal import Principal, PrincipalKind, PrincipalStatus
from lda.modules.identity.infrastructure.persistence.tables import principals
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError, InfrastructureError
from lda.shared.kernel.ids import PrincipalId

_UNIQUE_VIOLATION = "23505"


class SqlPrincipalRepository:
    """Tenant-bound: every statement carries the tenant predicate, and RLS enforces it again.

    Driver errors are translated and chained away: they embed statement parameters, which would
    put a display name into logs.
    """

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def add(self, principal: Principal) -> None:
        if principal.tenant_id != self._tx.tenant_id:
            raise InfrastructureError("principal_tenant_mismatch")
        try:
            self._tx.session.execute(
                insert(principals).values(
                    id=principal.id.value,
                    tenant_id=principal.tenant_id.value,
                    kind=principal.kind.value,
                    display_name=principal.display_name.value,
                    status=principal.status.value,
                    version=principal.version,
                    created_at=principal.created_at,
                    status_changed_at=principal.status_changed_at,
                )
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise ConflictError("principal_exists") from None
            raise InfrastructureError("identity_persistence_failed") from None
        except SQLAlchemyError:
            raise InfrastructureError("identity_persistence_failed") from None

    def get(self, principal_id: PrincipalId) -> Principal | None:
        try:
            row = (
                self._tx.session.execute(
                    select(principals).where(
                        principals.c.tenant_id == self._tx.tenant_id.value,
                        principals.c.id == principal_id.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
        except SQLAlchemyError:
            raise InfrastructureError("identity_persistence_failed") from None
        if row is None:
            return None
        return Principal(
            id=PrincipalId(row["id"]),
            tenant_id=self._tx.tenant_id,
            kind=PrincipalKind(row["kind"]),
            display_name=DisplayName(row["display_name"]),
            status=PrincipalStatus(row["status"]),
            version=row["version"],
            created_at=row["created_at"],
            status_changed_at=row["status_changed_at"],
        )

    def save(self, principal: Principal) -> None:
        statement = (
            update(principals)
            .where(
                principals.c.tenant_id == self._tx.tenant_id.value,
                principals.c.id == principal.id.value,
                principals.c.version == principal.version - 1,
            )
            .values(
                status=principal.status.value,
                version=principal.version,
                status_changed_at=principal.status_changed_at,
            )
        )
        try:
            changed = self._tx.execute_count(statement, {})
        except SQLAlchemyError:
            raise InfrastructureError("identity_persistence_failed") from None
        if changed != 1:
            raise ConflictError("principal_state_changed")
