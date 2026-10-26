from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.infrastructure.persistence.tables import policy_epochs
from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import InfrastructureError

_PERSISTENCE_FAILED = "access_control_persistence_failed"


class SqlPolicyEpochRepository:
    """The bound tenant's epoch. No row means 0; the first change creates the row at 1."""

    def __init__(self, tx: TenantTransaction) -> None:
        self._tx = tx

    def current(self) -> PolicyEpoch:
        try:
            value = self._tx.session.execute(
                select(policy_epochs.c.epoch).where(
                    policy_epochs.c.tenant_id == self._tx.tenant_id.value
                )
            ).scalar_one_or_none()
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None
        return PolicyEpoch(0 if value is None else int(value))

    def advance(self) -> PolicyEpoch:
        """Atomic `+1` (upsert); a concurrent advance waits on the row lock."""
        statement = (
            insert(policy_epochs)
            .values(tenant_id=self._tx.tenant_id.value, epoch=1)
            .on_conflict_do_update(
                index_elements=[policy_epochs.c.tenant_id],
                set_={"epoch": policy_epochs.c.epoch + 1},
            )
            .returning(policy_epochs.c.epoch)
        )
        try:
            value = self._tx.session.execute(statement).scalar_one()
        except SQLAlchemyError:
            raise InfrastructureError(_PERSISTENCE_FAILED) from None
        return PolicyEpoch(int(value))
