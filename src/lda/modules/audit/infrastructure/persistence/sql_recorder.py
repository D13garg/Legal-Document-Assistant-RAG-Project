from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.audit.application.entry_from_fact import audit_entry_of
from lda.modules.audit.infrastructure.persistence.tables import records
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError
from lda.shared.ports.audit import AuditFact


class SqlAuditRecorder:
    """Appends to `audit.records` through the action's OWN transaction (ADR-019 D1/D7).

    Used as the platform's `audit_factory`: `SqlAuditRecorder(tx)`. The row is written when
    `record` is called and commits or rolls back with the action. Any failure raises, so the
    action fails with it (fail-closed). Driver errors embed statement parameters and are chained
    away; a rejected fact is reported by code only.
    """

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def record(self, fact: AuditFact) -> None:
        entry = audit_entry_of(fact)
        entry.require_tenant(self._tx.tenant_id)
        try:
            self._tx.session.execute(
                insert(records).values(
                    tenant_id=entry.tenant_id.value,
                    actor=entry.actor.value if entry.actor is not None else None,
                    action=entry.action,
                    resource_type=entry.resource_type,
                    resource_id=entry.resource_id,
                    outcome=entry.outcome,
                    request_id=entry.request_id.value if entry.request_id is not None else None,
                    occurred_at=entry.occurred_at,
                    attributes=dict(entry.attributes),
                )
            )
        except SQLAlchemyError:
            raise InfrastructureError("audit_persistence_failed") from None
