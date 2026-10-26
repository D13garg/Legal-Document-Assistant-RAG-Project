"""In-memory Audit adapter with the same observable behaviour as `SqlAuditRecorder`.

Facts are validated by the same domain value, staged, and reach the shared store only on `commit`,
so rollback and tenant isolation can be tested without a database. A contract suite runs against
both implementations.
"""

from __future__ import annotations

from lda.modules.audit.application.entry_from_fact import audit_entry_of
from lda.modules.audit.domain.entry import AuditEntry
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.audit import AuditFact


class InMemoryAuditStore:
    """The committed records, shared by every recorder of one test."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def for_tenant(self, tenant_id: TenantId) -> list[AuditEntry]:
        return [e for e in self.entries if e.tenant_id == tenant_id]


class StagedAuditRecorder:
    """One unit of work's recorder: tenant-bound, validating, visible only after `commit`."""

    def __init__(self, tenant_id: TenantId, store: InMemoryAuditStore) -> None:
        self._tenant_id = tenant_id
        self._store = store
        self._staged: list[AuditEntry] = []

    def record(self, fact: AuditFact) -> None:
        entry = audit_entry_of(fact)
        entry.require_tenant(self._tenant_id)
        self._staged.append(entry)

    def commit(self) -> None:
        self._store.entries.extend(self._staged)
        self._staged = []

    def rollback(self) -> None:
        self._staged = []
