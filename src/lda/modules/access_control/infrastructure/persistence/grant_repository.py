from __future__ import annotations

from sqlalchemy import delete, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.access_control.domain.grant import Grant
from lda.modules.access_control.domain.resource import ActionId, ResourceRef
from lda.modules.access_control.infrastructure.persistence.tables import grants
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.kernel.ids import PrincipalId

_PERSISTENCE_FAILED = "access_control_persistence_failed"


class SqlGrantRepository:
    """Tenant-bound: every statement carries the tenant predicate, and RLS enforces it again.

    Driver errors are translated and chained away: they embed statement parameters (resource ids).
    """

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def add(self, grant: Grant) -> bool:
        if grant.tenant_id != self._tx.tenant_id:
            raise InfrastructureError("grant_tenant_mismatch")
        statement = (
            insert(grants)
            .values(
                tenant_id=grant.tenant_id.value,
                principal_id=grant.principal_id.value,
                resource_kind=grant.resource.kind,
                resource_id=grant.resource.id,
                action=grant.action.value,
                granted_at=grant.granted_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    grants.c.tenant_id,
                    grants.c.principal_id,
                    grants.c.resource_kind,
                    grants.c.resource_id,
                    grants.c.action,
                ]
            )
            .returning(grants.c.principal_id)
        )
        # RETURNING, not rowcount: the driver reports -1 for INSERT .. ON CONFLICT.
        try:
            return self._tx.session.execute(statement).first() is not None
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None

    def remove(self, principal_id: PrincipalId, resource: ResourceRef, action: ActionId) -> bool:
        statement = (
            delete(grants)
            .where(
                grants.c.tenant_id == self._tx.tenant_id.value,
                grants.c.principal_id == principal_id.value,
                grants.c.resource_kind == resource.kind,
                grants.c.resource_id == resource.id,
                grants.c.action == action.value,
            )
            .returning(grants.c.principal_id)
        )
        try:
            return self._tx.session.execute(statement).first() is not None
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None

    def remove_all_for_resource(self, resource: ResourceRef) -> int:
        statement = (
            delete(grants)
            .where(
                grants.c.tenant_id == self._tx.tenant_id.value,
                grants.c.resource_kind == resource.kind,
                grants.c.resource_id == resource.id,
            )
            .returning(grants.c.principal_id)
        )
        try:
            return len(self._tx.session.execute(statement).all())
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None

    def granted(
        self, principal_id: PrincipalId, action: ActionId, candidates: frozenset[ResourceRef]
    ) -> frozenset[ResourceRef]:
        if not candidates:
            return frozenset()
        statement = select(grants.c.resource_kind, grants.c.resource_id).where(
            grants.c.tenant_id == self._tx.tenant_id.value,
            grants.c.principal_id == principal_id.value,
            grants.c.action == action.value,
            tuple_(grants.c.resource_kind, grants.c.resource_id).in_(
                [(c.kind, c.id) for c in candidates]
            ),
        )
        try:
            rows = self._tx.session.execute(statement).all()
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None
        return frozenset(ResourceRef(kind, resource_id) for kind, resource_id in rows)
