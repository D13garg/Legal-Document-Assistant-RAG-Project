"""Base type for domain events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from lda.shared.kernel.ids import EventId, TenantId


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    """Immutable fact that something happened inside one tenant.

    Subclasses set a stable, unique `type_name` (e.g. "documents.DocumentUploaded") so that
    renaming a class never breaks persisted jobs. Events carry identifiers, never content.
    """

    type_name: ClassVar[str] = ""

    event_id: EventId
    tenant_id: TenantId
    occurred_at: datetime

    def __post_init__(self) -> None:
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
