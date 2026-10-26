from __future__ import annotations

from lda.modules.audit.domain.entry import AuditEntry
from lda.shared.ports.audit import AuditFact


def audit_entry_of(fact: AuditFact) -> AuditEntry:
    """Map the shared port's fact into the domain value, which validates it (ADR-019 D6).

    Raises `InvalidAuditEntry` for anything that is not an acceptable fact; the caller's
    transaction then fails with it (fail-closed).
    """
    return AuditEntry(
        tenant_id=fact.tenant_id,
        actor=fact.actor,
        action=fact.action,
        resource_type=fact.resource_type,
        resource_id=fact.resource_id,
        outcome=fact.outcome.value,
        request_id=fact.request_id,
        occurred_at=fact.occurred_at,
        attributes=fact.attributes,
    )
