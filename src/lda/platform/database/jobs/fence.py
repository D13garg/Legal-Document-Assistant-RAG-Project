from __future__ import annotations

from sqlalchemy import text

from lda.platform.database.transaction import TenantTransaction
from lda.shared.kernel.errors import ConflictError
from lda.shared.kernel.ids import JobId
from lda.shared.ports.clock import Clock

# FOR SHARE: a concurrent reclaim/claim (which updates the row) waits until this transaction ends,
# so effects verified here cannot commit after another worker has taken the job over.
_VERIFY = text(
    "SELECT 1 FROM platform.jobs WHERE id = :id AND tenant_id = :tenant "
    "AND status = 'running' AND fencing_token = :token AND lease_expires_at >= :now "
    "FOR SHARE"
)


class PostgresJobFence:
    """`JobFence` bound to one tenant transaction (ADR-012 amendment)."""

    def __init__(self, tx: TenantTransaction, clock: Clock) -> None:
        self._tx = tx
        self._clock = clock

    def verify(self, job_id: JobId, fencing_token: int) -> None:
        params = {
            "id": job_id.value,
            "tenant": self._tx.tenant_id.value,
            "token": fencing_token,
            "now": self._clock.now(),
        }
        if self._tx.session.execute(_VERIFY, params).first() is None:
            raise ConflictError("job_lease_lost")
