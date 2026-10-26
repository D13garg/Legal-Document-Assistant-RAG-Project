from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


@dataclass(frozen=True, kw_only=True)
class AuditFact:
    """What happened, to what, by whom. Identifiers and labels only; never document content."""

    tenant_id: TenantId
    actor: PrincipalId | None  # None for system work
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    request_id: RequestId | None
    occurred_at: datetime
    attributes: Mapping[str, str] = field(default_factory=dict)


class AuditRecorder(Protocol):
    def record(self, fact: AuditFact) -> None:
        """Append in the caller's transaction. Failure must fail the action (fail-closed)."""
        ...
